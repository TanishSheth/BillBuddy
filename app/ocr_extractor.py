"""
Local, offline receipt extraction — no AI API key required.

Runs Tesseract OCR on the receipt image, then pulls store name / date /
total out of the raw text with a handful of regex + positional heuristics.

This is intentionally the "free tier" of extraction: fast, works without
any configuration, and good enough for most clean printed receipts. It
reports a confidence flag per field so the caller (vlm.py) can decide
whether to escalate to a paid VLM when one is configured.
"""
import io
import logging
import re
from datetime import datetime, timedelta
from difflib import SequenceMatcher

from PIL import Image, ImageOps

from .schemas import ReceiptExtraction

logger = logging.getLogger(__name__)

try:
    import pytesseract
    TESSERACT_AVAILABLE = True
except ImportError:  # pragma: no cover - only hit if pytesseract isn't installed
    pytesseract = None
    TESSERACT_AVAILABLE = False


def tesseract_status() -> dict:
    """Report whether the Python binding and the tesseract binary are usable."""
    import shutil

    binary = shutil.which("tesseract")
    available = False
    version = None
    error = None
    if not TESSERACT_AVAILABLE:
        error = "pytesseract is not installed"
    elif not binary:
        error = "tesseract binary not found on PATH"
    else:
        try:
            version = str(pytesseract.get_tesseract_version())
            available = True
        except Exception as err:  # pragma: no cover - depends on host install
            error = str(err)[:200]
    return {
        "available": available,
        "pytesseract": TESSERACT_AVAILABLE,
        "binary": binary,
        "version": version,
        "error": error,
    }

try:
    import cv2
    import numpy as np
    CV2_AVAILABLE = True
except ImportError:
    cv2 = None
    np = None
    CV2_AVAILABLE = False

# PSM 11: sparse text. PSM 4 merges receipt columns; PSM 6 assumes one uniform block.
# Receipts vary between a sparse collection of fields and a single long column.
# We run both layouts below and keep the more complete read.
_TESSERACT_PSMS = ("--oem 3 --psm 11", "--oem 3 --psm 6")

# ---------------------------------------------------------------------------
# OCR letter/digit confusion repair
#
# Thermal/photo OCR very commonly swaps look-alike glyphs *inside* a numeric
# token: O/o/D <-> 0, I/l/| <-> 1, S/s <-> 5, B <-> 8, Z <-> 2, G <-> 6.
# This is the single biggest cause of "wrong amount" / "wrong date" reports.
# We only touch a token when it already contains at least one real digit and
# is shaped like an amount (NN.NN / NN,NN) or a date (NN/NN/NNNN etc.), so we
# never mangle ordinary words.
# ---------------------------------------------------------------------------
_DIGIT_FIX_MAP = str.maketrans({
    "O": "0", "o": "0", "D": "0",
    "I": "1", "l": "1", "|": "1",
    "S": "5", "s": "5",
    "B": "8",
    "Z": "2",
    "G": "6",
})
_MONEY_TOKEN_RE = re.compile(r"(?<![\w.])[0-9OoIlSsBZGD]{1,7}[.,][0-9OoIlSsBZGD]{2}(?![\w.])")
_DATE_TOKEN_RE = re.compile(
    r"(?<![\w./-])"
    r"[0-9OoIlSsBZGD]{1,2}\s*[/.\-]\s*[0-9OoIlSsBZGD]{1,2}\s*[/.\-]\s*[0-9OoIlSsBZGD]{2,4}"
    r"(?![\w./-])"
)


def _repair_money_confusable(match: "re.Match") -> str:
    token = match.group(0)
    if not any(ch.isdigit() for ch in token):
        return token  # no digit anchor at all — likely not a number, leave it
    if token[0].isdigit():
        return token.translate(_DIGIT_FIX_MAP)
    # Leading char is a confusable *letter* (S, O, ...) rather than a digit —
    # on a money token that's almost always a misread currency symbol
    # ("$12.99" -> "S12.99"), not a digit. Leave it alone so the currency
    # misread check downstream can still see it; only fix the numeral run.
    return token[0] + token[1:].translate(_DIGIT_FIX_MAP)


def _repair_date_confusable(match: "re.Match") -> str:
    token = match.group(0)
    if not any(ch.isdigit() for ch in token):
        return token
    # Fix confusable digits inside numeral runs only — leave separators intact.
    return re.sub(
        r"[0-9OoIlSsBZGD]+",
        lambda m: m.group(0).translate(_DIGIT_FIX_MAP),
        token,
    )


def _repair_ocr_digits(text: str) -> str:
    """Run once on raw OCR output before any field extraction."""
    if not text:
        return text
    text = _MONEY_TOKEN_RE.sub(_repair_money_confusable, text)
    text = _DATE_TOKEN_RE.sub(_repair_date_confusable, text)
    return text


_MIN_TESSERACT_CONF = 55.0
_MAX_PLAUSIBLE_TOTAL = 200000.0


# ---------------------------------------------------------------------------
# Image cleanup + OCR
# ---------------------------------------------------------------------------

def _pil_to_bgr(image_bytes: bytes):
    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.exif_transpose(img)
    if img.mode != "RGB":
        img = img.convert("RGB")
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def _order_quad(pts):
    pts = pts.reshape(4, 2).astype(np.float32)
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    return np.array(
        [pts[np.argmin(s)], pts[np.argmin(d)], pts[np.argmax(s)], pts[np.argmax(d)]],
        dtype=np.float32,
    )


def _find_document_quad(bgr):
    h, w = bgr.shape[:2]
    scale = 900 / max(h, w)
    small = cv2.resize(bgr, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else bgr
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(gray, 50, 150)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    page_area = small.shape[0] * small.shape[1]
    best = None
    best_area = 0.0
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < page_area * 0.12:
            continue
        peri = cv2.arcLength(cnt, True)
        approx = cv2.approxPolyDP(cnt, 0.02 * peri, True)
        if len(approx) == 4 and area > best_area:
            best = approx
            best_area = area
    if best is None:
        return None
    quad = _order_quad(best)
    if scale < 1:
        quad = quad / scale
    if cv2.contourArea(quad.reshape(-1, 1, 2)) < h * w * 0.12:
        return None
    return quad


def _warp_perspective(bgr, quad):
    tl, tr, br, bl = _order_quad(quad)
    width = int(max(np.linalg.norm(br - bl), np.linalg.norm(tr - tl)))
    height = int(max(np.linalg.norm(tr - br), np.linalg.norm(tl - bl)))
    width = max(width, 80)
    height = max(height, 80)
    dst = np.array(
        [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]],
        dtype=np.float32,
    )
    matrix = cv2.getPerspectiveTransform(np.array([tl, tr, br, bl], dtype=np.float32), dst)
    return cv2.warpPerspective(bgr, matrix, (width, height))


def _rotate_bound(bgr, angle: float, border_value=None):
    """Rotate by an arbitrary angle without cropping content off the edges
    (canvas grows to fit). Used for angles far outside what the narrow-band
    text-pixel deskew below is willing to touch."""
    h, w = bgr.shape[:2]
    cx, cy = w / 2.0, h / 2.0
    matrix = cv2.getRotationMatrix2D((cx, cy), angle, 1.0)
    cos, sin = abs(matrix[0, 0]), abs(matrix[0, 1])
    new_w = int((h * sin) + (w * cos))
    new_h = int((h * cos) + (w * sin))
    matrix[0, 2] += (new_w / 2.0) - cx
    matrix[1, 2] += (new_h / 2.0) - cy
    kwargs = {} if border_value is None else {"borderValue": border_value}
    return cv2.warpAffine(bgr, matrix, (new_w, new_h), **kwargs)


def _foreground_mask(bgr):
    """Binary mask + largest contour of the paper blob.

    Tries both Otsu polarities and keeps whichever gives a plausible paper
    blob. The original code guessed polarity from the mask's overall mean
    brightness, which is backwards whenever the paper is the *bright*
    object on a *dark* background (receipt on a dark counter/table) — it
    would invert a perfectly good mask into one that swallows almost the
    entire frame (background) instead of just the paper. Here we instead
    reject any candidate whose largest contour is suspiciously close to
    the full frame, since that's a sign the wrong polarity got picked.
    """
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    _, base = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    img_area = bgr.shape[0] * bgr.shape[1]
    best_mask, best_cnt, best_area = None, None, 0.0
    for candidate in (base, cv2.bitwise_not(base)):
        closed = cv2.morphologyEx(candidate, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
        contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            continue
        cnt = max(contours, key=cv2.contourArea)
        area = cv2.contourArea(cnt)
        if area >= img_area * 0.95:  # this polarity picked up the background, not the paper
            continue
        if area > best_area:
            best_mask, best_cnt, best_area = closed, cnt, area
    return best_mask, best_cnt, best_area


def _blob_tilt_angle(cnt) -> float:
    """Angle (degrees, folded to (-45, 45]) of the paper blob's long edge
    relative to horizontal — works for *any* rotation, not just small tilts."""
    rect = cv2.minAreaRect(cnt)
    box = cv2.boxPoints(rect)
    best_len, best_angle = -1.0, 0.0
    for i in range(4):
        p1, p2 = box[i], box[(i + 1) % 4]
        dx, dy = float(p2[0] - p1[0]), float(p2[1] - p1[1])
        length = (dx * dx + dy * dy) ** 0.5
        if length > best_len:
            best_len, best_angle = length, np.degrees(np.arctan2(dy, dx))
    angle = best_angle % 180
    if angle > 90:
        angle -= 180
    if angle > 45:
        angle -= 90
    elif angle <= -45:
        angle += 90
    return angle


def _straighten_and_crop(bgr):
    """Fallback path used when no clean 4-corner quad was found.

    First coarsely rotates the *whole* image by the paper blob's actual
    tilt angle — any angle, not capped like `_deskew` below — then crops
    to that blob's bounding box. `_deskew` still runs afterward to mop up
    the last degree or two of residual tilt from estimation noise.
    """
    mask, cnt, area = _foreground_mask(bgr)
    if cnt is None or area < bgr.shape[0] * bgr.shape[1] * 0.08:
        return bgr
    angle = _blob_tilt_angle(cnt)
    rotated = _rotate_bound(bgr, angle) if abs(angle) >= 0.5 else bgr
    mask2, cnt2, area2 = _foreground_mask(rotated)
    if cnt2 is None:
        return rotated
    x, y, w, h = cv2.boundingRect(cnt2)
    pad = 8
    x0, y0 = max(x - pad, 0), max(y - pad, 0)
    x1, y1 = min(x + w + pad, rotated.shape[1]), min(y + h + pad, rotated.shape[0])
    return rotated[y0:y1, x0:x1]


def _osd_rotation(gray_img) -> int:
    """0/90/180/270 the *content* needs rotating to read upright, via
    Tesseract's own orientation detector (OSD).

    EXIF only corrects for how the camera sensor was held; the geometric
    straightening above only corrects paper-edge tilt. Neither one knows
    which side of an already-straight rectangle is "up" — a receipt laid
    on the table sideways, or a perfectly deskewed scan that's upside
    down, needs this step. Low-confidence OSD reads are ignored rather
    than risking a wrong rotation.
    """
    if not TESSERACT_AVAILABLE:
        return 0
    try:
        osd = pytesseract.image_to_osd(gray_img, output_type=pytesseract.Output.DICT)
        conf = float(osd.get("orientation_conf", 0) or 0)
        if conf < 1.0:
            return 0
        return int(osd.get("rotate", 0)) % 360
    except Exception as exc:
        logger.info("OSD orientation check skipped: %s", exc)
        return 0


def _apply_osd_correction(variants: list):
    """Rotate every prepared variant together based on OSD read from the
    first one, so the gray/binary pair stay in sync."""
    if not variants:
        return variants
    angle = _osd_rotation(variants[0][1])
    rot_code = {
        90: cv2.ROTATE_90_CLOCKWISE,
        180: cv2.ROTATE_180,
        270: cv2.ROTATE_90_COUNTERCLOCKWISE,
    }.get(angle)
    if rot_code is None:
        return variants
    logger.info("OSD detected content rotated ~%d degrees, correcting", angle)
    return [(name, cv2.rotate(img, rot_code)) for name, img in variants]


def _crop_to_document(bgr):
    return _straighten_and_crop(bgr)


def _deskew(bgr):
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    inverted = cv2.bitwise_not(gray)
    thresh = cv2.threshold(inverted, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)[1]
    coords = np.column_stack(np.where(thresh > 0))
    if len(coords) < 50:
        return bgr
    angle = cv2.minAreaRect(coords)[-1]
    if angle < -45:
        angle = -(90 + angle)
    else:
        angle = -angle
    if abs(angle) < 0.4 or abs(angle) > 20:
        return bgr
    h, w = bgr.shape[:2]
    center = (w // 2, h // 2)
    matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
    return cv2.warpAffine(bgr, matrix, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


def _flatten_illumination(gray):
    blur = cv2.GaussianBlur(gray, (0, 0), sigmaX=25, sigmaY=25)
    blur = np.maximum(blur, 1)
    flat = cv2.divide(gray, blur, scale=255)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    return clahe.apply(flat)


def _remove_wrinkle_shadows(gray):
    """Flatten local crease shadows before adaptive thresholding.

    A broad morphological close estimates paper illumination while ignoring
    letter strokes. Dividing by that background handles the sharp, local
    gradients that a global deskew or a single Otsu threshold cannot fix.
    """
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (25, 25))
    background = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, kernel)
    return cv2.divide(gray, np.maximum(background, 1), scale=255)


def _soften_thermal_ink(gray):
    """Light speckle removal only. Unsharp + bilateral split thermal pin-dots (3→8, I→|)."""
    return cv2.medianBlur(gray, 3)


def _bridge_thermal_dots(img):
    """Horizontal close so 58mm/80mm heated-pin gaps join into strokes."""
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 1))
    if float(np.mean(img)) > 127:
        return cv2.bitwise_not(cv2.morphologyEx(cv2.bitwise_not(img), cv2.MORPH_CLOSE, kernel))
    return cv2.morphologyEx(img, cv2.MORPH_CLOSE, kernel)


def _sauvola(gray, window: int = 25, k: float = 0.2):
    if window % 2 == 0:
        window += 1
    mean = cv2.boxFilter(gray.astype(np.float32), -1, (window, window), normalize=True)
    sqmean = cv2.boxFilter((gray.astype(np.float32) ** 2), -1, (window, window), normalize=True)
    std = np.sqrt(np.maximum(sqmean - mean * mean, 0))
    thresh = mean * (1 + k * ((std / 128.0) - 1))
    return np.where(gray.astype(np.float32) > thresh, 255, 0).astype(np.uint8)


def _ensure_min_width(gray, min_width: int = 1400):
    if gray.shape[1] >= min_width:
        return gray
    scale = min_width / gray.shape[1]
    return cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)


def _looks_like_digital_capture(bgr) -> bool:
    """Phone/portal screenshots: dark UI, sharp contrast, no paper quad."""
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    mean = float(np.mean(gray))
    h, w = gray.shape[:2]
    if h < 40 or w < 40:
        return False
    border = np.concatenate([gray[:6, :].ravel(), gray[-6:, :].ravel(), gray[:, :6].ravel(), gray[:, -6:].ravel()])
    interior = gray[h // 10: h - h // 10, w // 10: w - w // 10]
    if interior.size == 0:
        return False
    if mean < 110:
        return True
    if float(np.mean(border)) < 95 and float(np.std(interior)) > 35:
        return True
    return False


def _prepare_screenshot_variants(bgr):
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    if float(np.mean(gray)) < 127:
        gray = cv2.bitwise_not(gray)
    gray = _ensure_min_width(gray)
    _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return _apply_osd_correction([("screen_gray", gray), ("screen_bin", otsu)])


def _prepare_paper_variants(bgr):
    quad = _find_document_quad(bgr)
    if quad is not None:
        bgr = _warp_perspective(bgr, quad)
    else:
        bgr = _straighten_and_crop(bgr)
    bgr = _deskew(bgr)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    gray = _flatten_illumination(gray)
    gray = _remove_wrinkle_shadows(gray)
    gray = _soften_thermal_ink(gray)
    gray = _bridge_thermal_dots(gray)
    gray = _ensure_min_width(gray)
    binary = _bridge_thermal_dots(_sauvola(gray))
    return _apply_osd_correction([("gray", gray), ("binary", binary)])


def _prepare_variants(image_bytes: bytes):
    bgr = _pil_to_bgr(image_bytes)
    if _looks_like_digital_capture(bgr):
        logger.info("OCR treating image as a digital screenshot (skip paper crop / lighting flatten)")
        return _prepare_screenshot_variants(bgr)
    return _prepare_paper_variants(bgr)


def _group_tokens_into_lines(data: dict) -> tuple[list[str], int, float, list[int], list[int], list[dict]]:
    """Join Tesseract tokens into rows while retaining receipt geometry."""
    n = len(data.get("text") or [])
    tokens = []
    confs: list[float] = []
    for i in range(n):
        word = str(data["text"][i] or "").strip()
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        if conf >= 0:
            confs.append(conf)
        if not word or conf < 0:
            continue
        try:
            tokens.append({
                "text": _repair_ocr_digits(word),
                "left": int(data["left"][i]),
                "top": int(data["top"][i]),
                "w": int(data["width"][i]),
                "h": max(int(data["height"][i]), 1),
            })
        except (TypeError, ValueError, KeyError):
            continue
    tokens.sort(key=lambda t: (t["top"], t["left"]))
    rows: list[dict] = []
    lefts: list[int] = []
    heights: list[int] = []
    for tok in tokens:
        cy = tok["top"] + tok["h"] / 2.0
        matched = None
        best_distance = None
        for row in rows:
            # A crease can lift the right-hand price away from its left-hand
            # label. Permit a modest, distance-aware vertical band while
            # favouring the closest row so adjacent receipt lines stay apart.
            dx = abs(tok["left"] - row["x"])
            predicted_cy = row["cy"] + row["slope"] * (tok["left"] - row["x"])
            tolerance = max(row["h"], tok["h"]) * 0.65 + min(20.0, dx * 0.04)
            distance = abs(cy - predicted_cy)
            if distance <= tolerance and (best_distance is None or distance < best_distance):
                matched = row
                best_distance = distance
        if matched is None:
            rows.append({"tokens": [tok], "cy": cy, "h": float(tok["h"]), "x": float(tok["left"]), "slope": 0.0})
        else:
            matched["tokens"].append(tok)
            count = len(matched["tokens"])
            matched["cy"] = (matched["cy"] * (count - 1) + cy) / count
            matched["h"] = max(matched["h"], float(tok["h"]))
            # Fit a gentle local text slope. Clamp it to avoid one noisy word
            # pulling the entire row into a neighbouring line.
            xs = [item["left"] + item["w"] / 2.0 for item in matched["tokens"]]
            ys = [item["top"] + item["h"] / 2.0 for item in matched["tokens"]]
            x_mean, y_mean = sum(xs) / len(xs), sum(ys) / len(ys)
            denom = sum((x - x_mean) ** 2 for x in xs)
            if denom > 1:
                slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / denom
                matched["slope"] = max(-0.18, min(0.18, slope))
                matched["x"] = x_mean
    rows.sort(key=lambda r: r["cy"])
    lines = []
    line_boxes: list[dict] = []
    for row in rows:
        row["tokens"].sort(key=lambda t: t["left"])
        line = " ".join(t["text"] for t in row["tokens"]).strip()
        if line:
            lines.append(line)
            lefts.append(row["tokens"][0]["left"])
            heights.append(int(round(row["h"])))
            line_boxes.append({
                "left": min(token["left"] for token in row["tokens"]),
                "top": min(token["top"] for token in row["tokens"]),
                "right": max(token["left"] + token["w"] for token in row["tokens"]),
                "bottom": max(token["top"] + token["h"] for token in row["tokens"]),
                "tokens": row["tokens"],
            })
    mean = sum(confs) / len(confs) if confs else 0.0
    return lines, len(tokens), mean, lefts, heights, line_boxes


def _tesseract_pass(image, psm: str) -> tuple[str, float, list[str], int, list[int], list[int], list[dict]]:
    empty = ("", 0.0, [], 0, [], [], [])
    if not TESSERACT_AVAILABLE:
        return empty
    try:
        data = pytesseract.image_to_data(image, config=psm, output_type=pytesseract.Output.DICT)
        lines, token_count, mean, lefts, heights, line_boxes = _group_tokens_into_lines(data)
        lines = [_repair_ocr_digits(l) for l in lines]
        text = _repair_ocr_digits("\n".join(lines))
        return text, mean, lines, token_count, lefts, heights, line_boxes
    except Exception as exc:
        logger.warning("Tesseract failed: %s", exc)
        return empty


def _extract_text_regions(binary) -> list[tuple[int, int, int, int]]:
    """Find likely text strips for a line-by-line OCR fallback.

    This deliberately operates after shadow normalization and thresholding.
    It does not try to straighten the entire sheet again: each crop is read as
    one line, so local waves and small perspective shifts do not make
    Tesseract's page-layout detector lose the row.
    """
    if binary is None or binary.size == 0:
        return []
    ink = cv2.bitwise_not(binary) if float(np.mean(binary)) > 127 else binary
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (20, 3))
    joined = cv2.dilate(ink, kernel, iterations=1)
    contours, _ = cv2.findContours(joined, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    height, width = binary.shape[:2]
    regions = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if w < max(30, width * 0.04) or h < 9 or h > height * 0.16:
            continue
        pad_x, pad_y = 10, 5
        regions.append((max(0, x - pad_x), max(0, y - pad_y),
                        min(width, x + w + pad_x), min(height, y + h + pad_y)))
    return sorted(regions, key=lambda region: (region[1], region[0]))


def _tesseract_local_line_pass(binary) -> tuple[str, float, list[str], int, list[int], list[int], list[dict]]:
    """OCR detected receipt strips with PSM 7, retaining page coordinates."""
    empty = ("", 0.0, [], 0, [], [], [])
    if not TESSERACT_AVAILABLE:
        return empty
    regions = _extract_text_regions(binary)
    if not regions:
        return empty
    merged = {key: [] for key in ("text", "conf", "left", "top", "width", "height")}
    for x0, y0, x1, y1 in regions:
        try:
            data = pytesseract.image_to_data(
                binary[y0:y1, x0:x1], config="--oem 3 --psm 7", output_type=pytesseract.Output.DICT,
            )
        except Exception as exc:
            logger.info("Local line OCR skipped: %s", exc)
            continue
        for index, text in enumerate(data.get("text") or []):
            merged["text"].append(text)
            merged["conf"].append(data.get("conf", [])[index])
            merged["left"].append(int(data.get("left", [])[index]) + x0)
            merged["top"].append(int(data.get("top", [])[index]) + y0)
            merged["width"].append(data.get("width", [])[index])
            merged["height"].append(data.get("height", [])[index])
    if not merged["text"]:
        return empty
    lines, token_count, mean, lefts, heights, line_boxes = _group_tokens_into_lines(merged)
    text = _repair_ocr_digits("\n".join(lines))
    return text, mean, lines, token_count, lefts, heights, line_boxes


_ANCHOR_RE = re.compile(
    r"\b(?:total|subtotal|sub-tot|tax|gst|hst|pst|vat|change|cash|qty|item|amount due|balance)\b",
    re.I,
)
_DATE_HINT_RE = re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-](?:\d{4}|\d{2})\b")
_MONEY_HINT_RE = re.compile(r"\d+\.\d{2}")


def _variant_score(text: str, mean_conf: float, token_count: int) -> float:
    """Prefer a full receipt (TOTAL/TAX/dates/money) over a sparse high-confidence hallucination."""
    if not (text or "").strip():
        return -1.0
    anchors = len(_ANCHOR_RE.findall(text))
    dates = len(_DATE_HINT_RE.findall(text))
    money = len(_MONEY_HINT_RE.findall(text))
    return (
        anchors * 14.0
        + dates * 10.0
        + min(money, 15) * 5.0
        + min(len(text), 2500) / 180.0
        + min(token_count, 120) * 0.25
        + mean_conf * 0.12
    )


def _run_ocr(image_bytes: bytes) -> tuple[str, float, list[str], list[int], list[int], list[dict]]:
    """Return OCR text, confidence, line text, and token-level receipt geometry."""
    empty = ("", 0.0, [], [], [], [])
    status = tesseract_status()
    if not status.get("available"):
        logger.warning("Local OCR unavailable: %s", status.get("error") or "tesseract missing")
        return empty

    if CV2_AVAILABLE:
        try:
            best = ("", -1.0, [], [], [], [], -1.0)
            for name, variant in _prepare_variants(image_bytes):
                for psm in _TESSERACT_PSMS:
                    text, conf, lines, ntok, lefts, heights, line_boxes = _tesseract_pass(variant, psm)
                    score = _variant_score(text, conf, ntok)
                    logger.info("OCR variant %s (%s) mean confidence %.1f score %.1f", name, psm, conf, score)
                    if score > best[6]:
                        best = (text, conf, lines, lefts, heights, line_boxes, score)
                # The binary image is best suited to contour extraction. This
                # adds a PSM-7 local-line candidate rather than replacing the
                # full-page PSM-11 read used for ordinary, flat receipts.
                if "bin" in name or name == "binary":
                    text, conf, lines, ntok, lefts, heights, line_boxes = _tesseract_local_line_pass(variant)
                    score = _variant_score(text, conf, ntok)
                    logger.info("OCR variant %s (local PSM 7) mean confidence %.1f score %.1f", name, conf, score)
                    if score > best[6]:
                        best = (text, conf, lines, lefts, heights, line_boxes, score)
            if best[6] >= 0:
                return best[0], max(best[1], 0.0), best[2], best[3], best[4], best[5]
        except Exception as exc:
            logger.warning("OpenCV cleanup failed, using basic OCR: %s", exc)

    try:
        img = Image.open(io.BytesIO(image_bytes))
        img = ImageOps.exif_transpose(img)
        img = img.convert("L")
        img = ImageOps.autocontrast(img)
        if img.width < 1500:
            scale = 1500 / img.width
            img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
        candidates = []
        for psm in _TESSERACT_PSMS:
            text, conf, lines, ntok, lefts, heights, line_boxes = _tesseract_pass(img, psm)
            candidates.append((text, conf, lines, lefts, heights, line_boxes, _variant_score(text, conf, ntok)))
        text, conf, lines, lefts, heights, line_boxes, _score = max(candidates, key=lambda item: item[6])
        return text, conf, lines, lefts, heights, line_boxes
    except Exception as exc:
        logger.warning("Local OCR failed: %s", exc)
        return empty


def _looks_implausible(extraction: ReceiptExtraction) -> bool:
    store = (extraction.store_name or "").strip().lower()
    if not store or store.startswith("unknown"):
        return True
    try:
        total = float(extraction.total_amount or 0)
    except (TypeError, ValueError):
        return True
    if total <= 0 or total > _MAX_PLAUSIBLE_TOTAL:
        return True
    return False


# ---------------------------------------------------------------------------
# Field extraction
# ---------------------------------------------------------------------------

_AMOUNT_RE = re.compile(r"(?<!\d)(\d{1,3}(?:,\d{3})*(?:\.\d{2})|\d+\.\d{2})(?!\d)")
# European convention: "." groups thousands, "," is the decimal point
# (e.g. "1.234,56"). Only tried when the US-style pattern above finds
# nothing on that line, so a normal US/UK line is never re-parsed.
_EURO_AMOUNT_RE = re.compile(r"(?<!\d)(\d{1,3}(?:\.\d{3})*,\d{2})(?!\d)")

_AMBIGUOUS_TOTALS = {"due", "net", "sum", "tot", "charged", "balance"}


def _norm_label_text(line: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", line.lower()).split())


def _prep_phrases(words: list[str], drop: set[str] | None = None) -> list[str]:
    drop = drop or set()
    seen: set[str] = set()
    out: list[str] = []
    for raw in words:
        phrase = _norm_label_text(raw)
        if not phrase or phrase in seen or phrase in drop:
            continue
        seen.add(phrase)
        out.append(phrase)
    out.sort(key=lambda s: (-len(s), s))
    return out


# Ignore-lists are checked first so tax / subtotal / change are never used as the total.
_TAX_PHRASES = _prep_phrases([
    "Tax", "Sales Tax", "VAT", "Value Added Tax", "GST", "Goods & Services Tax",
    "HST", "Harmonized Sales Tax", "PST", "Provincial Sales Tax", "QST",
    "Quebec Sales Tax", "CGST", "SGST", "IGST", "UTGST", "State Tax", "Local Tax",
    "City Tax", "County Tax", "Fed Tax", "Federal Tax", "Consumption Tax",
    "Service Tax", "Cess", "Swachh Bharat Cess", "Krishi Kalyan Cess", "Luxury Tax",
    "Excise", "Excise Duty", "Surcharge", "Addl Tax", "Additional Tax", "Levies",
    "Levy", "Govt Tax", "Tax Amt", "Tax Amount", "Total Tax", "Txs", "Tx",
    "Sales Tx", "Vat Amount", "Vat Amt", "Gst Amount", "Gst Amt", "Hst Amt",
    "Pst Amt", "Rounding Tax", "Non-Tax", "Taxable", "Tax 1", "Tax 2", "Tax A",
    "Tax B", "Fiscal Tax", "Municipal Tax", "Entertainment Tax",
    "sales tax", "custom duty", "customs duty", "taxation", "tariff",
])

_SUBTOTAL_PHRASES = _prep_phrases([
    "Subtotal", "Sub-Total", "Net Total", "Sub Tot", "Sub-Tot",
    "Sub Amt", "Sub Amount", "Net Amt", "Net Amount", "Before Tax", "Ex Tax",
    "Excl Tax", "Excluding Tax", "Pre-Tax", "Pre Tax", "Total Before Tax",
    "Items Total", "Goods Total", "Merchandise Total", "Mdse Total", "MDSE",
    "Line Total", "Items Subtotal", "Basket Total", "Subtotal Amount",
    "Subtotal Amt", "Amount Before Tax", "Net Items", "Product Total",
    "Menu Total", "Order Subtotal", "Ticket Subtotal", "Prev Total",
    "Preliminary Total", "Initial Total", "Base Amount", "Base Amt",
    "Taxable Amt", "Taxable Amount", "Untaxed Amount", "Untaxed Amt",
    "Net Order", "Value Before Tax", "Summary Subtotal", "Raw Total",
    "Sub Total Amount", "Sub-Total Amount", "Total Excl Tax",
    "pre-tax total", "running total", "section total", "interim total",
    "item total", "base value", "gross total",
])

_CHANGE_PHRASES = _prep_phrases([
    "Change", "Cash Change", "Change Due", "Tendered Change", "Balance Return",
    "Balance Returned", "Refund", "Refund Amount", "Amount Returned", "Cash Back",
    "Cashback", "Return Amt", "Return Amount", "Change Given", "Given Back",
    "Cash Return", "Cash Returned", "Change Cash", "Due Back", "Due To Customer",
    "Customer Change", "Dispensed", "Dispensed Amt", "Dispensed Amount",
    "Coin Change", "Notes Change", "Overpayment", "Remainder", "Residual",
    "Surplus", "Payout", "Cash Payout", "Tender Change", "Change Tendered",
    "Out Change", "Change Out", "Bal Return", "Net Change", "Final Change",
    "Leftover", "Leftover Cash", "Change Amount", "Change Amt", "Paid Out",
    "Paid Back", "Chg", "Change Tender",
])

_AMBIGUOUS_PAYMENT = {
    "cs", "cc", "c c", "crd", "dr", "qr", "p2p", "p2m",
    "bill", "bills", "notes", "coins", "charge", "card", "credit", "debit",
    "chip", "swipe", "insert", "tap", "plastic", "currency", "moneys",
    "greenbacks", "dbt", "wallet", "eft", "jcb",
}

_PAYMENT_PHRASES = _prep_phrases([
    "Cash", "Cash Tendered", "Cash Paid", "Cash Amount", "Cash Amt", "Csh",
    "Cashier Cash", "Hard Cash", "Physical Cash", "Cash Sale", "Cash Received",
    "Cash Recd", "Cash Rec'd", "Tendered", "Amount Tendered", "Amt Tendered",
    "Total Tendered", "Cash Total", "Paid Cash", "Paper Currency", "Cash Reg",
    "Register Cash", "Drawer Cash", "Cash In", "Cash Payment", "Cash Pymt",
    "Pymt Cash", "Payment Cash", "Hand Cash", "Cash Trans", "Cash Transaction",
    "Cash Drawer", "Counter Cash", "Store Cash", "Local Currency",
    "Cash Collected", "Collected Cash", "Liquid Cash", "Immediate Cash",
    "Cash Value", "Cash Net", "Cash Gross",
    "Visa Purchase", "Mastercard Purchase", "Debit Purchase", "Credit Purchase",
    "Card Purchase",
    "Credit Card", "Debit Card", "Visa", "Mastercard", "MasterCard", "Amex",
    "American Express", "Discover", "Rupay", "RuPay", "Maestro", "Diners",
    "Diners Club", "Card Charge", "Card Payment", "Card Pymt", "Credit Payment",
    "Debit Payment", "Card Sale", "EFTPOS", "POS Payment", "Terminal Payment",
    "Contactless", "Tap to Pay", "Apple Pay", "Google Pay", "GPay",
    "Samsung Pay", "Mobile Wallet", "Digital Wallet", "Online Payment",
    "Card Tendered", "Charged to Card", "Auth Code", "Approval Code",
    "Card Total", "Card Number", "Card No",
    "UPI", "UPI Payment", "BHIM", "BHIM UPI", "PhonePe", "Paytm", "Amazon Pay",
    "WhatsApp Pay", "Cred Pay", "QR Code", "QR Payment", "Scan QR", "Scan & Pay",
    "E-Wallet", "Wallet Payment", "Venmo", "PayPal", "Cash App", "Zelle",
    "Online Pymt", "Net Banking", "NetBanking", "Internet Banking", "E-Payment",
    "Electronic Payment", "IMPS", "NEFT", "RTGS", "Bank Transfer",
    "Direct Transfer", "Virtual Payment", "VPA", "UPI ID", "Payee VPA",
    "Instant Pay", "Mobile Pay", "Contactless Pay", "Digital Pay",
    "App Payment", "Electronic Cash",
    "Debit Sale", "Debit Pymt", "Debit Charge", "Debit Tendered", "EFT Debit",
    "Visa Debit", "Mastercard Debit", "RuPay Debit", "Interac", "Interac Debit",
    "Checking Card", "Savings Card", "ATM Card", "Check Card", "Cheque Card",
    "Bank Card", "Direct Debit", "Direct Pay", "POS Debit", "Terminal Debit",
    "Chip Debit", "Swipe Debit", "Pin Debit", "PIN Payment", "Debit Auth",
    "Debited", "Amount Debited", "Amt Debited", "Account Debit", "Local Debit",
    "Retail Debit", "Store Debit", "Card Debit", "Secured Debit", "Instant Debit",
    "Debit Trans", "Debit Transaction", "DB Card", "Dbt Card", "D-Card",
    "DR Card", "Debit Net", "Debit Total",
], drop=_AMBIGUOUS_PAYMENT)

# Leftover / statement carry-over — not the amount paid on this receipt.
_REMAINING_PHRASES = _prep_phrases([
    "Balance Remaining", "Remaining Balance", "Bal Remaining", "Bal Due",
    "Amount Remaining", "Amt Remaining", "Amount Outstanding", "Amt Outstanding",
    "Outstanding Balance", "Outstanding Bal", "Outstanding Amt", "Outstanding Amount",
    "Arrears", "Total Arrears", "Unpaid Balance", "Unpaid Bal", "Unpaid Amt",
    "Unpaid Amount", "Carried Forward", "Balance C/F", "Bal C/F", "B/F",
    "Balance B/F", "Pending Balance", "Pending Bal", "Pending Amount", "Pending Amt",
    "Acct Bal", "Closing Bal", "Amount Left", "Amt Left", "Remaining Charge", "Remaining Cost",
    "Residual Balance", "Residual Bal", "Deferred Balance", "Carryover",
], drop={"due", "bal", "balance", "remainder", "owed"})

_SAVINGS_PHRASES = _prep_phrases([
    "promotional savings", "price reduction", "price cut", "mark down", "markdown",
    "total savings", "you saved", "you save", "member savings", "store credit", "savings",
    "savings:", "bogo", "buy one get one",
    "discount", "allowance", "deduction", "rebate", "concession", "waiver", "coupon",
])

_META_PHRASES = _prep_phrases([
    "Address", "Addr", "Location", "Store Location", "Store Address", "Venue",
    "Site", "Branch Address", "Shop Address", "Outlet Address", "Business Address",
    "Corporate Address", "Registered Office", "Reg Office", "Office Address",
    "Head Office", "Premise", "Premises", "Postal Address", "Mailing Address",
    "ZIP", "Zip Code", "Postal Code", "Postcode", "PIN Code",
    "Phone", "Phone No", "Phone No.", "Telephone", "Tel No", "Tel No.",
    "Contact No", "Contact No.", "Mobile", "Mob No", "Mob No.", "Cell No",
    "Cell No.", "Hotline", "Customer Care", "Cust Care", "Support No",
    "Support Number", "Helpline", "Office Phone", "Branch Phone", "Store Phone",
    "Shop Phone", "Landline", "Land Line", "Toll Free", "Toll-Free", "Call Us",
    "Email", "E-Mail", "Email Address", "E-Mail Address", "Email Addr",
    "Mail Address", "Electronic Mail", "Contact Email", "Support Email",
    "Customer Email", "Feedback Email", "Office Email", "Store Email",
    "Branch Email", "Corporate Email", "Info Email", "Email Us", "Help Email",
    "Sales Email", "Billing Email", "Accounts Email", "Email ID", "E-Mail ID",
])

_TOTAL_PHRASES = _prep_phrases([
    "amount before rounding", "Total", "Grand Total", "Gross Total",
    "Total Amount", "Amt Due", "Amount Due", "Total Due", "Balance Due",
    "Payable", "Total Payable", "Net Payable", "Final Total", "Final Amount",
    "Invoice Total", "Receipt Total", "Order Total", "Sale Total", "Total Sale",
    "Total Charged", "Amnt Due", "Grand Tot",
    "Net Tot", "Tot Amt", "Tot Amount", "G. Total", "G.Total", "Summary Total",
    "Check Total", "Bill Total", "Statement Total", "Amount To Pay", "Total To Pay",
    "Total Price", "Net Price", "Total Charge", "Combined Total", "Aggregate Total",
    "End Total", "Total Cost", "Total Value", "Transaction Total", "To Pay",
    # Common OCR misreads of TOTAL on thermal paper.
    "T0TAL", "TOIAL", "TOTAI", "TOT AL", "TOTL", "TTL",
    "sum total", "gross amount", "due balance", "amount paid", "pay this",
    "aggregate", "tally", "Net Due", "Left to Pay", "Amount Owed", "Amount Owed",
    "Rent", "Rental", "Rent Payment", "Rent Amt", "Rent Amount", "House Rent",
    "Room Rent", "Office Rent", "Shop Rent", "Monthly Rent", "Lease", "Account Balance",
    "Current Charges", "Current Charge", "Statement Balance", "Closing Balance",
    "Lease Payment", "Lease Amt", "Lease Amount", "Lease Rent", "Tenancy",
    "Tenancy Fee", "Licence Fee", "License Fee", "Occupancy Cost", "HRA",
    "House Rent Allowance", "Base Rent", "Basic Rent", "Net Rent", "Gross Rent",
    "Flat Rent", "Apartment Rent", "Studio Rent", "Space Rental", "Venue Rental",
    "Equipment Rental", "Car Rental", "Vehicle Rent", "Hire Charges", "Hire Fee",
    "Rental Fee", "Rental Charge", "Rental Cost", "Property Rent",
    "Real Estate Rent", "Contract Rent", "Agreed Rent", "Current Rent",
    "Advance Rent", "Prepaid Rent", "Rent Due", "Rent Paid", "Rent Collected",
    "Rental Pymt",
], drop=_AMBIGUOUS_TOTALS)


def _phrase_in_line(norm: str, phrase: str) -> bool:
    compact_line = norm.replace(" ", "")
    compact_phrase = phrase.replace(" ", "").replace("-", "")
    if len(compact_phrase) >= 6 and compact_phrase in compact_line:
        return True
    pat = r"(?<![a-z0-9])" + re.escape(phrase).replace(r"\ ", r"[\s\-]*") + r"(?![a-z0-9])"
    return bool(re.search(pat, norm))


def _line_has_any_phrase(norm: str, phrases: list[str]) -> bool:
    return any(_phrase_in_line(norm, p) for p in phrases)


_IGNORE_KINDS = ("savings", "tax", "subtotal", "change", "payment", "remaining", "meta")


def _classify_amount_line(line: str) -> str | None:
    """savings | tax | subtotal | change | payment | remaining | meta | total | None"""
    norm = _norm_label_text(line)
    if not norm:
        return None
    if re.search(r"\*\s*\d{3,4}\b", line) and _parse_amounts(line):
        return "payment"
    if _line_has_any_phrase(norm, _SAVINGS_PHRASES):
        return "savings"
    if _line_has_any_phrase(norm, _TAX_PHRASES):
        return "tax"
    if _line_has_any_phrase(norm, _SUBTOTAL_PHRASES):
        return "subtotal"
    if _line_has_any_phrase(norm, _CHANGE_PHRASES):
        return "change"
    if _line_has_any_phrase(norm, _PAYMENT_PHRASES):
        return "payment"
    if _line_has_any_phrase(norm, _REMAINING_PHRASES):
        return "remaining"
    if _line_has_any_phrase(norm, _META_PHRASES):
        return "meta"
    if _line_has_any_phrase(norm, _TOTAL_PHRASES):
        if re.search(r"points?\s+balance|reward\s+balance|card\s+balance", norm):
            return None
        return "total"
    return None


def _parse_amounts(text: str) -> list[float]:
    amounts = []
    matches = _AMOUNT_RE.findall(text)
    if matches:
        for raw in matches:
            try:
                amounts.append(float(raw.replace(",", "")))
            except ValueError:
                continue
        return amounts
    # Fall back to European decimal-comma parsing only if the US-style
    # pattern found nothing on this line at all.
    for raw in _EURO_AMOUNT_RE.findall(text):
        try:
            amounts.append(float(raw.replace(".", "").replace(",", ".")))
        except ValueError:
            continue
    return amounts


_REGISTER_LINE_RE = re.compile(
    r"(?:\*\s*\d{3,})"
    r"|(?:\b(?:FS|STR|REG|TRN|CSHR|TRANS|CASHIER|OP#)\d*\b)"
    r"|(?:\d{3,}/\d{2,}/\d{2,})",
    re.I,
)


def _is_register_line(line: str) -> bool:
    """Register / store-system footers like *1935 FS67/003/006 — not a bill total."""
    return bool(_REGISTER_LINE_RE.search(line or ""))


def _plausible_receipt_total(amount: float) -> bool:
    return amount is not None and 0 < amount <= _MAX_PLAUSIBLE_TOTAL


def _amount_on_same_row(line: str) -> float | None:
    """Rightmost money figure on this row only (the amount column)."""
    amounts = _parse_amounts(line)
    return amounts[-1] if amounts else None


def _find_total_by_geometry(lines: list[str], line_boxes: list[dict]) -> tuple[float | None, bool]:
    """Rank every detected money token using its label and receipt position.

    Receipts normally put the payable amount in a right-aligned column near
    the bottom. Keeping the OCR bounding boxes lets us distinguish that value
    from similarly formatted item prices, tax, tendered cash, and loyalty
    balances without relying on one literal spelling of "TOTAL".
    """
    if not line_boxes:
        return None, False
    page_right = max((box.get("right", 0) for box in line_boxes), default=1)
    page_bottom = max((box.get("bottom", 0) for box in line_boxes), default=1)
    candidates: list[tuple[float, float, bool]] = []
    money_tokens: list[tuple[dict, dict, float]] = []

    for index, box in enumerate(line_boxes):
        line = lines[index] if index < len(lines) else ""
        if not line or _is_register_line(line):
            continue
        kind = _classify_amount_line(line)
        if kind in _IGNORE_KINDS:
            continue
        tokens = box.get("tokens") or []
        for token in tokens:
            amounts = _parse_amounts(str(token.get("text") or ""))
            if not amounts:
                continue
            for amount in amounts:
                if _plausible_receipt_total(amount):
                    money_tokens.append((token, box, amount))
            right_fraction = min((token.get("left", 0) + token.get("w", 0)) / max(page_right, 1), 1.0)
            lower_fraction = min((token.get("top", 0) + token.get("h", 0)) / max(page_bottom, 1), 1.0)
            score = right_fraction * 24.0 + lower_fraction * 16.0
            is_total_row = kind == "total"
            if is_total_row:
                score += 100.0
            # Rightmost numeric value is usually the amount column when a row
            # contains quantity, unit price, and line total.
            if token.get("left", 0) + token.get("w", 0) >= box.get("right", 0) - 3:
                score += 8.0
            for amount in amounts:
                if _plausible_receipt_total(amount):
                    candidates.append((score, amount, is_total_row))

    # On a creased receipt, the left-side label and right-side amount can be
    # read as separate rows. Pair a total label with a price to its right using
    # a slope-aware vertical band instead of demanding the same y-coordinate.
    for index, label_box in enumerate(line_boxes):
        label_line = lines[index] if index < len(lines) else ""
        if not label_line or _classify_amount_line(label_line) != "total":
            continue
        label_y = (label_box.get("top", 0) + label_box.get("bottom", 0)) / 2.0
        label_right = label_box.get("right", 0)
        line_height = max(label_box.get("bottom", 0) - label_box.get("top", 0), 1)
        for token, _amount_box, amount in money_tokens:
            token_left = token.get("left", 0)
            if token_left < label_right - 4:
                continue
            token_y = token.get("top", 0) + token.get("h", 0) / 2.0
            dx = token_left - label_right
            # A 5% rise/fall across the row plus almost one line height covers
            # local waviness without connecting unrelated receipt sections.
            vertical_band = min(70.0, line_height * 0.9 + dx * 0.05)
            dy = abs(token_y - label_y)
            if dy > vertical_band:
                continue
            right_fraction = min((token_left + token.get("w", 0)) / max(page_right, 1), 1.0)
            proximity = max(0.0, 1.0 - (dy / max(vertical_band, 1.0)))
            candidates.append((120.0 + right_fraction * 16.0 + proximity * 12.0, amount, True))

    if not candidates:
        return None, False
    best_score, best_amount, is_total_row = max(candidates, key=lambda candidate: candidate[0])
    # A label-free candidate is a useful form suggestion, but is deliberately
    # low confidence so the VLM/manual-review fallback still runs.
    # Bottom-right money usually scores ~40; accept from 34 so the form prefills.
    if is_total_row or best_score >= 34.0:
        return best_amount, is_total_row
    return None, False


def _find_total_bottom_fallback(
    lines: list[str], line_boxes: list[dict] | None = None,
) -> tuple[float | None, bool]:
    """Last resort: largest plausible amount in the bottom third of the receipt."""
    boxes = line_boxes or []
    if boxes:
        page_bottom = max((box.get("bottom", 0) for box in boxes), default=1)
        cutoff = page_bottom * 0.68
        amounts: list[float] = []
        for index, box in enumerate(boxes):
            if box.get("bottom", 0) < cutoff:
                continue
            line = lines[index] if index < len(lines) else ""
            if _is_register_line(line) or _classify_amount_line(line) in _IGNORE_KINDS:
                continue
            for amount in _parse_amounts(line):
                if _plausible_receipt_total(amount):
                    amounts.append(amount)
        if amounts:
            return max(amounts), False
    tail_amounts: list[float] = []
    for line in lines[-8:]:
        if _is_register_line(line) or _classify_amount_line(line) in _IGNORE_KINDS:
            continue
        for amount in _parse_amounts(line):
            if _plausible_receipt_total(amount):
                tail_amounts.append(amount)
    if tail_amounts:
        return max(tail_amounts), False
    return None, False


def _find_total(lines: list[str], line_boxes: list[dict] | None = None) -> tuple[float | None, bool]:
    """Find the payable amount, preferring full geometry-aware OCR evidence."""
    geometric_amount, geometric_confident = _find_total_by_geometry(lines, line_boxes or [])
    if geometric_amount is not None:
        return geometric_amount, geometric_confident

    # Geometry is unavailable only with an OCR backend that did not return
    # bounding boxes. Preserve the conservative text-only fallback.
    found = None
    for index, line in enumerate(lines):
        if _is_register_line(line):
            continue
        if _classify_amount_line(line) != "total":
            continue
        amount = _amount_on_same_row(line)
        if amount is not None and _plausible_receipt_total(amount):
            found = amount
            continue
        # Narrow receipts occasionally put "TOTAL" and its right-column amount
        # on separate OCR rows. Only accept the immediately following bare amount
        # and never override a real same-row TOTAL found later.
        if index + 1 < len(lines):
            next_line = lines[index + 1]
            if _classify_amount_line(next_line) is None and not _is_register_line(next_line):
                amounts = _parse_amounts(next_line)
                if len(amounts) == 1 and _plausible_receipt_total(amounts[0]):
                    found = amounts[0]
    if found is not None:
        return found, True
    return _find_total_bottom_fallback(lines, line_boxes)


_DATE_LABEL_RE = re.compile(
    r"(?i)\b(?:"
    r"date|dated|dt|txn\s*date|tran(?:saction)?\s*date|purchase\s*date|"
    r"bill\s*date|invoice\s*date|sale\s*date|order\s*date|printed|"
    r"time\s*stamp|timestamp"
    r")\b\s*[:#-]?\s*"
)

_DATE_PATTERNS = [
    # ISO first — unambiguous.
    r"\b(\d{4}-\d{2}-\d{2})(?:[T\s]\d{1,2}:\d{2}(?::\d{2})?)?\b",
    r"\b(\d{4}/\d{1,2}/\d{1,2})\b",
    # Compact YYYYMMDD when clearly year-led.
    r"\b((?:19|20)\d{2}[01]\d[0-3]\d)\b",
    # Numeric with optional OCR spaces around separators.
    r"\b(\d{1,2}\s*/\s*\d{1,2}\s*/\s*\d{4})\b",
    r"\b(\d{1,2}\s*/\s*\d{1,2}\s*/\s*\d{2})(?!\d)",
    r"\b(\d{1,2}\s*-\s*\d{1,2}\s*-\s*\d{4})\b",
    r"\b(\d{1,2}\s*-\s*\d{1,2}\s*-\s*\d{2})(?!\d)",
    # Dot-separated, common outside the US (13.09.2026 / 13.09.26).
    r"\b(\d{1,2}\s*\.\s*\d{1,2}\s*\.\s*\d{4})\b",
    r"\b(\d{1,2}\s*\.\s*\d{1,2}\s*\.\s*\d{2})(?!\d)",
    # OCR sometimes uses | or l as a separator.
    r"\b(\d{1,2}\s*[|l]\s*\d{1,2}\s*[|l]\s*\d{2,4})\b",
    # Month name forms, with optional ordinal / apostrophe year.
    r"\b([A-Za-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+'?\d{2,4})\b",
    r"\b(\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9}\.?,?\s+'?\d{2,4})\b",
    # Compact day+month+year without spaces: 13SEP2026 / 13SEP26.
    r"\b(\d{1,2}(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\d{2,4})\b",
    r"\b((?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\d{1,2},?\d{2,4})\b",
]

_MONTH_NAMES = (
    "jan feb mar apr may jun jul aug sep oct nov dec "
    "january february march april june july august september october november december"
).split()


def _prefers_day_first(text: str) -> bool:
    """DD/MM order is the convention almost everywhere except the US.

    Reuses the same locale anchors as currency detection so a UK/EU/India/
    Canada receipt with an ambiguous 2-digit-year date (e.g. 03/04/25) isn't
    silently read as US month-first when it plainly isn't a US receipt.
    """
    blob = text or ""
    upper = blob.upper()
    if _US_STATE_ZIP.search(blob):
        return False
    if (
        _INR_MARK.search(blob)
        or _CA_POSTAL.search(blob)
        or _UK_POSTAL.search(upper)
        or "VAT" in upper
        or "GST" in upper
        or "GSTIN" in upper
    ):
        return True
    return False


def _normalize_date_raw(raw: str) -> str:
    """Collapse OCR noise so parsers see a clean date token."""
    text = (raw or "").strip().rstrip(",.;")
    text = text.replace("'", "")
    text = re.sub(r"(?i)(\d)(st|nd|rd|th)\b", r"\1", text)
    text = re.sub(r"\s*[|l]\s*", "/", text)
    text = re.sub(r"\s*([/.\-])\s*", r"\1", text)
    text = re.sub(r"\s+", " ", text)
    # Compact 20260913 → 2026-09-13
    if re.fullmatch(r"(?:19|20)\d{6}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    # Compact 13SEP2026 / SEP132026
    m = re.fullmatch(
        r"(?i)(\d{1,2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|SEPT|OCT|NOV|DEC)[A-Z]*(\d{2,4})",
        text.replace(" ", ""),
    )
    if m:
        return f"{m.group(1)} {m.group(2)[:3]} {m.group(3)}"
    m = re.fullmatch(
        r"(?i)(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|SEPT|OCT|NOV|DEC)[A-Z]*(\d{1,2}),?(\d{2,4})",
        text.replace(" ", ""),
    )
    if m:
        return f"{m.group(1)[:3]} {m.group(2)} {m.group(3)}"
    return text


def _try_parse_date(raw: str, day_first: bool = False) -> datetime | None:
    raw = _normalize_date_raw(raw)
    if not raw:
        return None
    candidates: list[str] = []

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        candidates.append("%Y-%m-%d")
    elif re.fullmatch(r"\d{4}/\d{1,2}/\d{1,2}", raw):
        candidates += ["%Y/%m/%d", "%Y/%d/%m"]
    elif "/" in raw or "-" in raw or "." in raw:
        sep = "/" if "/" in raw else ("-" if "-" in raw else ".")
        parts = raw.split(sep)
        if len(parts) != 3:
            return None
        # Dot-separated dates are almost always day-first regardless of locale.
        prefer_day_first = day_first or sep == "."
        if len(parts[0]) == 4 and parts[0].isdigit():
            candidates += [f"%Y{sep}%m{sep}%d", f"%Y{sep}%d{sep}%m"]
        elif len(parts[-1]) == 2:
            pair = [f"%d{sep}%m{sep}%y", f"%m{sep}%d{sep}%y"]
            candidates += pair if prefer_day_first else pair[::-1]
        else:
            pair = [f"%d{sep}%m{sep}%Y", f"%m{sep}%d{sep}%Y"]
            candidates += pair if prefer_day_first else pair[::-1]
    else:
        candidates += [
            "%B %d %Y", "%b %d %Y", "%B %d %y", "%b %d %y",
            "%d %B %Y", "%d %b %Y", "%d %B %y", "%d %b %y",
            "%B %d, %Y", "%b %d, %Y", "%B %d, %y", "%b %d, %y",
        ]

    for fmt in candidates:
        try:
            parsed = datetime.strptime(raw, fmt)
            # strptime %y uses 1969-2068 window; clamp odd century for receipts.
            if parsed.year < 1990 and len(raw) <= 12:
                parsed = parsed.replace(year=parsed.year + 100)
            return parsed
        except ValueError:
            continue
    return None


def _date_candidate_score(
    parsed: datetime,
    *,
    labeled: bool,
    headerish: bool,
    now: datetime,
) -> tuple:
    """Higher is better. Prefer labeled / header dates and nearer-to-today values."""
    age_days = abs((now.date() - parsed.date()).days)
    return (
        3 if labeled else 0,
        1 if headerish else 0,
        -age_days,
        -parsed.toordinal(),
    )


def _iter_date_raws(text: str) -> list[tuple[str, bool, bool]]:
    """Yield (raw, labeled, headerish) date tokens from receipt text."""
    if not text:
        return []
    repaired = _repair_ocr_digits(text)
    # Soft-normalize separators before regex so spaced OCR still matches.
    soft = re.sub(r"(\d)\s*([/.\-|])\s*(\d)", r"\1\2\3", repaired)
    soft = re.sub(r"(\d)\s*([/.\-|])\s*(\d)", r"\1\2\3", soft)

    lines = [ln.strip() for ln in soft.splitlines() if ln.strip()]
    found: list[tuple[str, bool, bool]] = []
    seen: set[str] = set()

    def add(raw: str, labeled: bool, headerish: bool) -> None:
        key = raw.strip().lower()
        if not key or key in seen:
            return
        seen.add(key)
        found.append((raw, labeled, headerish))

    # Pass 1: tokens after an explicit date label (highest signal).
    for match in _DATE_LABEL_RE.finditer(soft):
        window = soft[match.end(): match.end() + 40]
        for pattern in _DATE_PATTERNS:
            hit = re.search(pattern, window, flags=re.IGNORECASE)
            if hit:
                add(hit.group(1), True, True)
                break

    # Pass 2: every pattern across the receipt; mark early lines as headerish.
    header_blob = "\n".join(lines[:16])
    for pattern in _DATE_PATTERNS:
        for raw in re.findall(pattern, soft, flags=re.IGNORECASE):
            headerish = raw in header_blob or bool(re.search(re.escape(raw), header_blob, re.I))
            add(raw, False, headerish)

    return found


def _find_date(text: str) -> tuple[str | None, bool]:
    now = datetime.now()
    earliest_sane = now - timedelta(days=365 * 5)
    latest_sane = now + timedelta(days=2)  # timezone / clock skew on registers
    day_first = _prefers_day_first(text)

    best: tuple | None = None
    best_parsed: datetime | None = None
    for raw, labeled, headerish in _iter_date_raws(text):
        parsed = _try_parse_date(raw, day_first=day_first)
        if not parsed:
            # If locale guess failed, try the opposite order before giving up.
            parsed = _try_parse_date(raw, day_first=not day_first)
        if not parsed or not (earliest_sane <= parsed <= latest_sane):
            continue
        score = _date_candidate_score(parsed, labeled=labeled, headerish=headerish, now=now)
        if best is None or score > best:
            best = score
            best_parsed = parsed

    if best_parsed is None:
        return None, False
    return best_parsed.strftime("%Y-%m-%d"), True


def _drop_child_promo_lines(
    lines: list[str],
    lefts: list[int] | None = None,
    boxes: list[dict] | None = None,
) -> tuple[list[str], list[int] | None, list[dict] | None]:
    """Skip indented BOGO / You saved children and $0.00 promo rows when picking a total."""
    if not lines:
        return lines, lefts, boxes
    lefts_in = lefts or [0] * len(lines)
    span = max(max(lefts_in) if lefts_in else 1, 1)
    out_lines: list[str] = []
    out_lefts: list[int] = []
    out_boxes: list[dict] = []
    for index, line in enumerate(lines):
        left = lefts_in[index] if index < len(lefts_in) else 0
        if left > span * 0.22 and re.search(r"you saved|saved:|bogo|\$0\.00|\b0\.00\b", line, re.I):
            continue
        if re.search(r"you saved", line, re.I):
            continue
        amounts = _parse_amounts(line)
        if amounts and all(abs(a) < 0.005 for a in amounts) and _classify_amount_line(line) not in ("total", "subtotal", "tax"):
            continue
        out_lines.append(line)
        out_lefts.append(left)
        if boxes and index < len(boxes):
            out_boxes.append(boxes[index])
    return out_lines, (out_lefts if lefts is not None else None), (out_boxes or None)


_HEADER_LINE_COUNT = 12
_SHORT_ALIAS_MAX = 5
_NEAR_EXACT_RATIO = 90
_WORD_RE = re.compile(r"[a-z0-9]+(?:'[a-z]+)?", re.I)

# Real retail chains only — never product/demo names. Used to canonicalize OCR
# header guesses (and phone/store# hard matches). Users also learn vendors via
# AccountVendor after they save a bill.
_KNOWN_VENDORS = [
    # —— United States ——
    {"name": "Publix", "phones": {"5613625305"}, "stores": {"0553", "1708", "553"}, "aliases": ["publix"]},
    {"name": "Walmart", "phones": set(), "stores": set(), "aliases": ["walmart", "wal-mart"], "labels": ["great value"]},
    {"name": "Costco", "phones": set(), "stores": set(), "aliases": ["costco"], "labels": ["kirkland"]},
    {"name": "Trader Joe's", "phones": set(), "stores": set(), "aliases": ["trader joe", "trader joes"], "labels": ["trader joe"]},
    {"name": "Target", "phones": set(), "stores": set(), "aliases": ["target"], "labels": ["good & gather", "good and gather"]},
    {"name": "Kroger", "phones": set(), "stores": set(), "aliases": ["kroger"]},
    {"name": "Whole Foods", "phones": set(), "stores": set(), "aliases": ["whole foods", "whole foods market"], "labels": ["365 whole"]},
    {"name": "Aldi", "phones": set(), "stores": set(), "aliases": ["aldi"]},
    {"name": "Safeway", "phones": set(), "stores": set(), "aliases": ["safeway"]},
    {"name": "CVS", "phones": set(), "stores": set(), "aliases": ["cvs", "cvs pharmacy"]},
    {"name": "Walgreens", "phones": set(), "stores": set(), "aliases": ["walgreens"]},
    {"name": "Amazon", "phones": set(), "stores": set(), "aliases": ["amazon", "amazon.com", "amazon fresh", "whole foods amazon"]},
    {"name": "Starbucks", "phones": set(), "stores": set(), "aliases": ["starbucks"]},
    {"name": "McDonald's", "phones": set(), "stores": set(), "aliases": ["mcdonalds", "mcdonald's", "mcd"]},
    {"name": "Chipotle", "phones": set(), "stores": set(), "aliases": ["chipotle"]},
    {"name": "Dunkin'", "phones": set(), "stores": set(), "aliases": ["dunkin", "dunkin donuts", "dunkin'"]},
    {"name": "Home Depot", "phones": set(), "stores": set(), "aliases": ["home depot", "the home depot"]},
    {"name": "Best Buy", "phones": set(), "stores": set(), "aliases": ["best buy", "bestbuy"]},
    {"name": "Uber", "phones": set(), "stores": set(), "aliases": ["uber", "uber trip"]},
    {"name": "Lyft", "phones": set(), "stores": set(), "aliases": ["lyft"]},
    {"name": "Shell", "phones": set(), "stores": set(), "aliases": ["shell"]},
    {"name": "Exxon", "phones": set(), "stores": set(), "aliases": ["exxon", "exxonmobil", "mobil"]},
    {"name": "Chevron", "phones": set(), "stores": set(), "aliases": ["chevron"]},
    {"name": "7-Eleven", "phones": set(), "stores": set(), "aliases": ["7-eleven", "7 eleven", "seven eleven"]},
    {"name": "Sam's Club", "phones": set(), "stores": set(), "aliases": ["sams club", "sam's club"]},
    {"name": "BJ's Wholesale", "phones": set(), "stores": set(), "aliases": ["bjs", "bj's", "bjs wholesale"]},
    {"name": "Meijer", "phones": set(), "stores": set(), "aliases": ["meijer"]},
    {"name": "H-E-B", "phones": set(), "stores": set(), "aliases": ["h-e-b", "heb"]},
    {"name": "Albertsons", "phones": set(), "stores": set(), "aliases": ["albertsons", "albertson's"]},
    {"name": "Ralphs", "phones": set(), "stores": set(), "aliases": ["ralphs"]},
    {"name": "Vons", "phones": set(), "stores": set(), "aliases": ["vons"]},
    {"name": "Food Lion", "phones": set(), "stores": set(), "aliases": ["food lion"]},
    {"name": "Harris Teeter", "phones": set(), "stores": set(), "aliases": ["harris teeter"]},
    {"name": "Wegmans", "phones": set(), "stores": set(), "aliases": ["wegmans"]},
    {"name": "Sprouts", "phones": set(), "stores": set(), "aliases": ["sprouts", "sprouts farmers market"]},
    {"name": "WinCo Foods", "phones": set(), "stores": set(), "aliases": ["winco", "winco foods"]},
    {"name": "ShopRite", "phones": set(), "stores": set(), "aliases": ["shoprite", "shop rite"]},
    {"name": "Stop & Shop", "phones": set(), "stores": set(), "aliases": ["stop & shop", "stop and shop"]},
    {"name": "Giant Eagle", "phones": set(), "stores": set(), "aliases": ["giant eagle"]},
    {"name": "Piggly Wiggly", "phones": set(), "stores": set(), "aliases": ["piggly wiggly"]},
    {"name": "Rite Aid", "phones": set(), "stores": set(), "aliases": ["rite aid", "riteaid"]},
    {"name": "Lowe's", "phones": set(), "stores": set(), "aliases": ["lowes", "lowe's"]},
    {"name": "Menards", "phones": set(), "stores": set(), "aliases": ["menards"]},
    {"name": "Ace Hardware", "phones": set(), "stores": set(), "aliases": ["ace hardware"]},
    {"name": "Walmart Neighborhood Market", "phones": set(), "stores": set(), "aliases": ["neighborhood market"]},
    {"name": "Dollar General", "phones": set(), "stores": set(), "aliases": ["dollar general"]},
    {"name": "Dollar Tree", "phones": set(), "stores": set(), "aliases": ["dollar tree"]},
    {"name": "Family Dollar", "phones": set(), "stores": set(), "aliases": ["family dollar"]},
    {"name": "Costco Gas", "phones": set(), "stores": set(), "aliases": ["costco gas", "costco fuel"]},
    {"name": "Circle K", "phones": set(), "stores": set(), "aliases": ["circle k", "circlek"]},
    {"name": "Wawa", "phones": set(), "stores": set(), "aliases": ["wawa"]},
    {"name": "Sheetz", "phones": set(), "stores": set(), "aliases": ["sheetz"]},
    {"name": "QuikTrip", "phones": set(), "stores": set(), "aliases": ["quiktrip", "qt"]},
    {"name": "RaceTrac", "phones": set(), "stores": set(), "aliases": ["racetrac", "race trac"]},
    {"name": "Speedway", "phones": set(), "stores": set(), "aliases": ["speedway"]},
    {"name": "Marathon", "phones": set(), "stores": set(), "aliases": ["marathon", "marathon petroleum"]},
    {"name": "Sunoco", "phones": set(), "stores": set(), "aliases": ["sunoco"]},
    {"name": "Burger King", "phones": set(), "stores": set(), "aliases": ["burger king"]},
    {"name": "Wendy's", "phones": set(), "stores": set(), "aliases": ["wendys", "wendy's"]},
    {"name": "Taco Bell", "phones": set(), "stores": set(), "aliases": ["taco bell"]},
    {"name": "Subway", "phones": set(), "stores": set(), "aliases": ["subway"]},
    {"name": "Panera Bread", "phones": set(), "stores": set(), "aliases": ["panera", "panera bread"]},
    {"name": "Chick-fil-A", "phones": set(), "stores": set(), "aliases": ["chick-fil-a", "chickfila", "chick fil a"]},
    {"name": "KFC", "phones": set(), "stores": set(), "aliases": ["kfc", "kentucky fried"]},
    {"name": "Domino's", "phones": set(), "stores": set(), "aliases": ["dominos", "domino's"]},
    {"name": "Pizza Hut", "phones": set(), "stores": set(), "aliases": ["pizza hut"]},
    {"name": "Papa John's", "phones": set(), "stores": set(), "aliases": ["papa johns", "papa john's"]},
    {"name": "Olive Garden", "phones": set(), "stores": set(), "aliases": ["olive garden"]},
    {"name": "Applebee's", "phones": set(), "stores": set(), "aliases": ["applebees", "applebee's"]},
    {"name": "Chili's", "phones": set(), "stores": set(), "aliases": ["chilis", "chili's"]},
    {"name": "DoorDash", "phones": set(), "stores": set(), "aliases": ["doordash", "door dash"]},
    {"name": "Grubhub", "phones": set(), "stores": set(), "aliases": ["grubhub", "grub hub"]},
    {"name": "Instacart", "phones": set(), "stores": set(), "aliases": ["instacart"]},
    {"name": "Uber Eats", "phones": set(), "stores": set(), "aliases": ["uber eats"]},
    {"name": "Netflix", "phones": set(), "stores": set(), "aliases": ["netflix"]},
    # —— India ——
    {"name": "Reliance Fresh", "phones": set(), "stores": set(), "aliases": ["reliance fresh", "reliance retail"]},
    {"name": "Reliance Smart", "phones": set(), "stores": set(), "aliases": ["reliance smart", "smart bazaar"]},
    {"name": "DMart", "phones": set(), "stores": set(), "aliases": ["dmart", "d-mart", "avenue supermarts"]},
    {"name": "Big Bazaar", "phones": set(), "stores": set(), "aliases": ["big bazaar", "bigbazaar"]},
    {"name": "More Retail", "phones": set(), "stores": set(), "aliases": ["more supermarket", "more megastore", "more retail"]},
    {"name": "Nature's Basket", "phones": set(), "stores": set(), "aliases": ["natures basket", "nature's basket"]},
    {"name": "Spencer's", "phones": set(), "stores": set(), "aliases": ["spencers", "spencer's"]},
    {"name": "Blinkit", "phones": set(), "stores": set(), "aliases": ["blinkit", "grofers"]},
    {"name": "Zepto", "phones": set(), "stores": set(), "aliases": ["zepto"]},
    {"name": "Swiggy", "phones": set(), "stores": set(), "aliases": ["swiggy", "swiggy instamart"]},
    {"name": "Zomato", "phones": set(), "stores": set(), "aliases": ["zomato"]},
    {"name": "BigBasket", "phones": set(), "stores": set(), "aliases": ["bigbasket", "big basket"]},
    {"name": "JioMart", "phones": set(), "stores": set(), "aliases": ["jiomart", "jio mart"]},
    {"name": "Apollo Pharmacy", "phones": set(), "stores": set(), "aliases": ["apollo pharmacy", "apollo"]},
    {"name": "MedPlus", "phones": set(), "stores": set(), "aliases": ["medplus", "med plus"]},
    {"name": "Indian Oil", "phones": set(), "stores": set(), "aliases": ["indian oil", "indianoil", "iocl"]},
    {"name": "HP Petrol", "phones": set(), "stores": set(), "aliases": ["hindustan petroleum", "hp petrol", "hpcl"]},
    {"name": "IRCTC", "phones": set(), "stores": set(), "aliases": ["irctc"]},
    {"name": "Cafe Coffee Day", "phones": set(), "stores": set(), "aliases": ["cafe coffee day", "ccd"]},
    {"name": "Reliance Trends", "phones": set(), "stores": set(), "aliases": ["reliance trends"]},
    {"name": "Reliance Digital", "phones": set(), "stores": set(), "aliases": ["reliance digital"]},
    {"name": "Reliance Jewels", "phones": set(), "stores": set(), "aliases": ["reliance jewels"]},
    {"name": "Jio Store", "phones": set(), "stores": set(), "aliases": ["jio store", "jio center"]},
    {"name": "Star Bazaar", "phones": set(), "stores": set(), "aliases": ["star bazaar", "starbazaar"]},
    {"name": "Hypercity", "phones": set(), "stores": set(), "aliases": ["hypercity"]},
    {"name": "Nilgiris", "phones": set(), "stores": set(), "aliases": ["nilgiris"]},
    {"name": "Heritage Fresh", "phones": set(), "stores": set(), "aliases": ["heritage fresh"]},
    {"name": "Ratnadeep", "phones": set(), "stores": set(), "aliases": ["ratnadeep"]},
    {"name": "Vishal Mega Mart", "phones": set(), "stores": set(), "aliases": ["vishal mega mart", "vishal megamart"]},
    {"name": "Metro Cash & Carry", "phones": set(), "stores": set(), "aliases": ["metro cash", "metro cash and carry", "metro wholesale"]},
    {"name": "Spar India", "phones": set(), "stores": set(), "aliases": ["spar india", "spar hypermarket"]},
    {"name": "Lulu Hypermarket", "phones": set(), "stores": set(), "aliases": ["lulu", "lulu hypermarket", "lulu mall"]},
    {"name": "Flipkart", "phones": set(), "stores": set(), "aliases": ["flipkart"]},
    {"name": "Amazon India", "phones": set(), "stores": set(), "aliases": ["amazon.in", "amazon india"]},
    {"name": "Myntra", "phones": set(), "stores": set(), "aliases": ["myntra"]},
    {"name": "Ajio", "phones": set(), "stores": set(), "aliases": ["ajio"]},
    {"name": "Nykaa", "phones": set(), "stores": set(), "aliases": ["nykaa"]},
    {"name": "Tata 1mg", "phones": set(), "stores": set(), "aliases": ["1mg", "tata 1mg", "one mg"]},
    {"name": "PharmEasy", "phones": set(), "stores": set(), "aliases": ["pharmeasy", "pharm easy"]},
    {"name": "Netmeds", "phones": set(), "stores": set(), "aliases": ["netmeds"]},
    {"name": "Wellness Forever", "phones": set(), "stores": set(), "aliases": ["wellness forever"]},
    {"name": "Guardian Pharmacy", "phones": set(), "stores": set(), "aliases": ["guardian pharmacy"]},
    {"name": "Bharat Petroleum", "phones": set(), "stores": set(), "aliases": ["bharat petroleum", "bpcl", "bharatgas"]},
    {"name": "Nayara Energy", "phones": set(), "stores": set(), "aliases": ["nayara", "nayara energy"]},
    {"name": "Shell India", "phones": set(), "stores": set(), "aliases": ["shell india"]},
    {"name": "IndiGo", "phones": set(), "stores": set(), "aliases": ["indigo airlines", "goindigo", "interglobe aviation"]},
    {"name": "Air India", "phones": set(), "stores": set(), "aliases": ["air india", "airindia"]},
    {"name": "SpiceJet", "phones": set(), "stores": set(), "aliases": ["spicejet", "spice jet"]},
    {"name": "Vistara", "phones": set(), "stores": set(), "aliases": ["vistara"]},
    {"name": "Ola", "phones": set(), "stores": set(), "aliases": ["ola", "ola cabs", "ola electric"]},
    {"name": "Rapido", "phones": set(), "stores": set(), "aliases": ["rapido"]},
    {"name": "Uber India", "phones": set(), "stores": set(), "aliases": ["uber india"]},
    {"name": "Starbucks India", "phones": set(), "stores": set(), "aliases": ["starbucks india", "tata starbucks"]},
    {"name": "Barista", "phones": set(), "stores": set(), "aliases": ["barista"]},
    {"name": "Chaayos", "phones": set(), "stores": set(), "aliases": ["chaayos"]},
    {"name": "Chai Point", "phones": set(), "stores": set(), "aliases": ["chai point", "chaipoint"]},
    {"name": "Domino's India", "phones": set(), "stores": set(), "aliases": ["dominos india", "jubilant foodworks"]},
    {"name": "Pizza Hut India", "phones": set(), "stores": set(), "aliases": ["pizza hut india"]},
    {"name": "McDonald's India", "phones": set(), "stores": set(), "aliases": ["mcdonalds india", "mcdonald's india"]},
    {"name": "KFC India", "phones": set(), "stores": set(), "aliases": ["kfc india"]},
    {"name": "Burger King India", "phones": set(), "stores": set(), "aliases": ["burger king india", "bk india"]},
    {"name": "Subway India", "phones": set(), "stores": set(), "aliases": ["subway india"]},
    {"name": "Haldiram's", "phones": set(), "stores": set(), "aliases": ["haldirams", "haldiram's"]},
    {"name": "Bikanervala", "phones": set(), "stores": set(), "aliases": ["bikanervala"]},
    {"name": "Faasos", "phones": set(), "stores": set(), "aliases": ["faasos", "rebel foods"]},
    {"name": "Behrouz Biryani", "phones": set(), "stores": set(), "aliases": ["behrouz", "behrouz biryani"]},
    {"name": "BookMyShow", "phones": set(), "stores": set(), "aliases": ["bookmyshow", "book my show"]},
    {"name": "PVR INOX", "phones": set(), "stores": set(), "aliases": ["pvr inox", "pvr cinemas", "inox movies"]},
    {"name": "Cinepolis India", "phones": set(), "stores": set(), "aliases": ["cinepolis india", "cinepolis"]},
    {"name": "Decathlon India", "phones": set(), "stores": set(), "aliases": ["decathlon india"]},
    {"name": "IKEA India", "phones": set(), "stores": set(), "aliases": ["ikea india"]},
    {"name": "Westside", "phones": set(), "stores": set(), "aliases": ["westside"]},
    {"name": "Pantaloons", "phones": set(), "stores": set(), "aliases": ["pantaloons"]},
    {"name": "Lifestyle Stores", "phones": set(), "stores": set(), "aliases": ["lifestyle stores", "lifestyle india"]},
    {"name": "Shoppers Stop", "phones": set(), "stores": set(), "aliases": ["shoppers stop"]},
    {"name": "Croma", "phones": set(), "stores": set(), "aliases": ["croma"]},
    {"name": "Vijay Sales", "phones": set(), "stores": set(), "aliases": ["vijay sales"]},
    # —— Australia ——
    {"name": "Woolworths", "phones": set(), "stores": set(), "aliases": ["woolworths", "woolies"]},
    {"name": "Coles", "phones": set(), "stores": set(), "aliases": ["coles"]},
    {"name": "ALDI Australia", "phones": set(), "stores": set(), "aliases": ["aldi australia"]},
    {"name": "IGA", "phones": set(), "stores": set(), "aliases": ["iga", "independent grocers"]},
    {"name": "Bunnings", "phones": set(), "stores": set(), "aliases": ["bunnings", "bunnings warehouse"]},
    {"name": "Officeworks", "phones": set(), "stores": set(), "aliases": ["officeworks"]},
    {"name": "Chemist Warehouse", "phones": set(), "stores": set(), "aliases": ["chemist warehouse"]},
    {"name": "Priceline", "phones": set(), "stores": set(), "aliases": ["priceline", "priceline pharmacy"]},
    {"name": "JB Hi-Fi", "phones": set(), "stores": set(), "aliases": ["jb hi-fi", "jb hifi", "jbhifi"]},
    {"name": "Kmart Australia", "phones": set(), "stores": set(), "aliases": ["kmart australia"]},
    {"name": "Kmart", "phones": set(), "stores": set(), "aliases": ["kmart"]},
    {"name": "Hungry Jack's", "phones": set(), "stores": set(), "aliases": ["hungry jacks", "hungry jack's"]},
    {"name": "Guzman y Gomez", "phones": set(), "stores": set(), "aliases": ["guzman y gomez", "gyg"]},
    {"name": "Uber Eats Australia", "phones": set(), "stores": set(), "aliases": ["uber eats australia"]},
    {"name": "Menulog", "phones": set(), "stores": set(), "aliases": ["menulog"]},
    {"name": "Caltex", "phones": set(), "stores": set(), "aliases": ["caltex"]},
    {"name": "BP Australia", "phones": set(), "stores": set(), "aliases": ["bp australia"]},
    {"name": "Harris Farm Markets", "phones": set(), "stores": set(), "aliases": ["harris farm", "harris farm markets"]},
    {"name": "FoodWorks", "phones": set(), "stores": set(), "aliases": ["foodworks", "food works"]},
    {"name": "Drakes Supermarkets", "phones": set(), "stores": set(), "aliases": ["drakes", "drakes supermarket"]},
    {"name": "Foodland", "phones": set(), "stores": set(), "aliases": ["foodland"]},
    {"name": "SPAR Australia", "phones": set(), "stores": set(), "aliases": ["spar australia"]},
    {"name": "Costco Australia", "phones": set(), "stores": set(), "aliases": ["costco australia"]},
    {"name": "Target Australia", "phones": set(), "stores": set(), "aliases": ["target australia"]},
    {"name": "Big W", "phones": set(), "stores": set(), "aliases": ["big w", "bigw"]},
    {"name": "The Reject Shop", "phones": set(), "stores": set(), "aliases": ["the reject shop", "reject shop"]},
    {"name": "Catch.com.au", "phones": set(), "stores": set(), "aliases": ["catch.com.au", "catch australia"]},
    {"name": "Myer", "phones": set(), "stores": set(), "aliases": ["myer"]},
    {"name": "David Jones", "phones": set(), "stores": set(), "aliases": ["david jones"]},
    {"name": "Harvey Norman", "phones": set(), "stores": set(), "aliases": ["harvey norman"]},
    {"name": "The Good Guys", "phones": set(), "stores": set(), "aliases": ["the good guys", "good guys"]},
    {"name": "Bing Lee", "phones": set(), "stores": set(), "aliases": ["bing lee"]},
    {"name": "Rebel Sport", "phones": set(), "stores": set(), "aliases": ["rebel sport"]},
    {"name": "Amart Sports", "phones": set(), "stores": set(), "aliases": ["amart sports"]},
    {"name": "Decathlon Australia", "phones": set(), "stores": set(), "aliases": ["decathlon australia"]},
    {"name": "IKEA Australia", "phones": set(), "stores": set(), "aliases": ["ikea australia"]},
    {"name": "Supercheap Auto", "phones": set(), "stores": set(), "aliases": ["supercheap auto", "super cheap auto"]},
    {"name": "Repco", "phones": set(), "stores": set(), "aliases": ["repco"]},
    {"name": "Autobarn", "phones": set(), "stores": set(), "aliases": ["autobarn"]},
    {"name": "Ampol", "phones": set(), "stores": set(), "aliases": ["ampol"]},
    {"name": "7-Eleven Australia", "phones": set(), "stores": set(), "aliases": ["7-eleven australia"]},
    {"name": "United Petroleum", "phones": set(), "stores": set(), "aliases": ["united petroleum"]},
    {"name": "Metro Petroleum", "phones": set(), "stores": set(), "aliases": ["metro petroleum"]},
    {"name": "Liberty Oil", "phones": set(), "stores": set(), "aliases": ["liberty oil", "liberty convenience"]},
    {"name": "McDonald's Australia", "phones": set(), "stores": set(), "aliases": ["mcdonalds australia", "maccas"]},
    {"name": "KFC Australia", "phones": set(), "stores": set(), "aliases": ["kfc australia"]},
    {"name": "Domino's Australia", "phones": set(), "stores": set(), "aliases": ["dominos australia"]},
    {"name": "Pizza Hut Australia", "phones": set(), "stores": set(), "aliases": ["pizza hut australia"]},
    {"name": "Subway Australia", "phones": set(), "stores": set(), "aliases": ["subway australia"]},
    {"name": "Nando's", "phones": set(), "stores": set(), "aliases": ["nandos", "nando's"]},
    {"name": "Grill'd", "phones": set(), "stores": set(), "aliases": ["grilld", "grill'd"]},
    {"name": "Boost Juice", "phones": set(), "stores": set(), "aliases": ["boost juice"]},
    {"name": "Gloria Jean's", "phones": set(), "stores": set(), "aliases": ["gloria jeans", "gloria jean's"]},
    {"name": "The Coffee Club", "phones": set(), "stores": set(), "aliases": ["the coffee club", "coffee club"]},
    {"name": "Starbucks Australia", "phones": set(), "stores": set(), "aliases": ["starbucks australia"]},
    {"name": "Zambrero", "phones": set(), "stores": set(), "aliases": ["zambrero"]},
    {"name": "Oporto", "phones": set(), "stores": set(), "aliases": ["oporto"]},
    {"name": "Red Rooster", "phones": set(), "stores": set(), "aliases": ["red rooster"]},
    {"name": "Crust Pizza", "phones": set(), "stores": set(), "aliases": ["crust pizza"]},
    {"name": "DoorDash Australia", "phones": set(), "stores": set(), "aliases": ["doordash australia"]},
    {"name": "Deliveroo", "phones": set(), "stores": set(), "aliases": ["deliveroo"]},
    {"name": "Qantas", "phones": set(), "stores": set(), "aliases": ["qantas"]},
    {"name": "Virgin Australia", "phones": set(), "stores": set(), "aliases": ["virgin australia", "virgin air"]},
    {"name": "Jetstar", "phones": set(), "stores": set(), "aliases": ["jetstar"]},
    {"name": "Tigerair", "phones": set(), "stores": set(), "aliases": ["tigerair", "tiger air"]},
    {"name": "Sydney Trains", "phones": set(), "stores": set(), "aliases": ["sydney trains", "opal card"]},
    {"name": "Metro Trains Melbourne", "phones": set(), "stores": set(), "aliases": ["metro trains melbourne", "myki"]},
    {"name": "TransLink", "phones": set(), "stores": set(), "aliases": ["translink", "go card"]},
    {"name": "Event Cinemas", "phones": set(), "stores": set(), "aliases": ["event cinemas"]},
    {"name": "Hoyts", "phones": set(), "stores": set(), "aliases": ["hoyts"]},
    {"name": "Village Cinemas", "phones": set(), "stores": set(), "aliases": ["village cinemas"]},
    {"name": "Dan Murphy's", "phones": set(), "stores": set(), "aliases": ["dan murphys", "dan murphy's"]},
    {"name": "BWS", "phones": set(), "stores": set(), "aliases": ["bws"]},
    {"name": "Liquorland", "phones": set(), "stores": set(), "aliases": ["liquorland"]},
    {"name": "First Choice Liquor", "phones": set(), "stores": set(), "aliases": ["first choice liquor"]},
    {"name": "TerryWhite Chemmart", "phones": set(), "stores": set(), "aliases": ["terrywhite", "terry white chemmart", "chemmart"]},
    {"name": "Blooms The Chemist", "phones": set(), "stores": set(), "aliases": ["blooms the chemist"]},
]

_PHONE_RE = re.compile(r"\(?\d{3}\)?[\s.\-]*\d{3}[\s.\-]*\d{4}")
_STORE_NUM_RE = re.compile(r"\bstore\s*#?\s*(\d{3,5})\b", re.I)
_ADDRESSY_RE = re.compile(r"\b(rd|road|st|street|ave|avenue|blvd|square|hwy|dr|drive|ln|lane|pkwy|parkway)\b", re.I)


def _phones_in_text(text: str) -> set[str]:
    return {re.sub(r"\D", "", m.group(0)) for m in _PHONE_RE.finditer(text or "")}


def _store_ids_in_text(text: str) -> set[str]:
    found = set()
    for m in _STORE_NUM_RE.finditer(text or ""):
        found.add(m.group(1).lstrip("0") or m.group(1))
        found.add(m.group(1))
    return found


def _word_tokens(text: str) -> list[str]:
    return [m.group(0).lower() for m in _WORD_RE.finditer(text or "")]


def _compact_alnum(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _header_lines(lines: list[str]) -> list[str]:
    return list(lines[:_HEADER_LINE_COUNT])


def _header_fingerprint(lines: list[str]) -> str:
    return _norm_label_text(" ".join(_header_lines(lines)))[:120]


def receipt_store_fingerprint(lines: list[str], text: str | None = None) -> dict:
    blob = text or "\n".join(lines)
    return {
        "phones": sorted(_phones_in_text(blob)),
        "store_ids": sorted(_store_ids_in_text(blob)),
        "header_key": _header_fingerprint(lines),
    }


def _token_ratio(a: str, b: str) -> int:
    if not a or not b:
        return 0
    if a == b:
        return 100
    return int(SequenceMatcher(None, a, b).ratio() * 100)


def _best_token_match(alias: str, tokens: list[str]) -> int:
    """Compare alias to whole tokens / consecutive token phrases — never mid-word slices."""
    parts = _word_tokens(alias)
    if not parts or not tokens:
        return 0
    n = len(parts)
    target = " ".join(parts)
    if n == 1:
        needle = parts[0]
        return max((_token_ratio(needle, tok) for tok in tokens), default=0)
    best = 0
    for i in range(len(tokens) - n + 1):
        window = " ".join(tokens[i:i + n])
        best = max(best, _token_ratio(target, window))
        if best >= 99:
            return 100
    return best


def _exact_alias_in_tokens(alias: str, tokens: list[str]) -> bool:
    parts = _word_tokens(alias)
    if not parts:
        return False
    n = len(parts)
    for i in range(len(tokens) - n + 1):
        if tokens[i:i + n] == parts:
            return True
    return False


def vendor_name_key(name: str) -> str:
    return _compact_alnum(name)[:80]


def _gazetteer_header_score(name: str, header_text: str) -> int:
    """How strongly a saved store name appears in the receipt header (tokens only)."""
    name_parts = _word_tokens(name)
    tokens = _word_tokens(header_text)
    if not name_parts or not tokens:
        return 0
    compact = _compact_alnum(name)
    if len(compact) <= _SHORT_ALIAS_MAX:
        return 100 if _exact_alias_in_tokens(name, tokens) else 0
    if _exact_alias_in_tokens(name, tokens):
        return 100
    name_norm = _norm_label_text(name)
    for line in (header_text or "").splitlines():
        line_norm = _norm_label_text(line)
        if not line_norm:
            continue
        ratio = _token_ratio(name_norm, line_norm)
        if ratio >= _NEAR_EXACT_RATIO:
            return ratio
    overlap = 0
    for n in range(min(len(name_parts), len(tokens)), 0, -1):
        ngrams = {" ".join(name_parts[i:i + n]) for i in range(len(name_parts) - n + 1)}
        for i in range(len(tokens) - n + 1):
            if " ".join(tokens[i:i + n]) in ngrams:
                overlap = n
                break
        if overlap:
            break
    if overlap >= 2:
        return 100
    return _best_token_match(name, tokens)


def _hard_vendor_from_directory(text: str) -> str | None:
    phones = _phones_in_text(text)
    store_ids = _store_ids_in_text(text)
    for vendor in _KNOWN_VENDORS:
        if phones & vendor.get("phones", set()):
            return vendor["name"]
        if store_ids & vendor.get("stores", set()):
            return vendor["name"]
    return None


def _canonicalize_from_header(header_text: str) -> str | None:
    """Map a header-zone string to a directory name. Never searches the receipt body."""
    tokens = _word_tokens(header_text)
    header_l = (header_text or "").lower()
    for vendor in _KNOWN_VENDORS:
        for alias in vendor.get("aliases", []):
            compact = _compact_alnum(alias)
            if len(compact) <= _SHORT_ALIAS_MAX:
                if _exact_alias_in_tokens(alias, tokens):
                    return vendor["name"]
                continue
            if _exact_alias_in_tokens(alias, tokens):
                return vendor["name"]
            if _best_token_match(alias, tokens) >= _NEAR_EXACT_RATIO:
                return vendor["name"]
        for label in vendor.get("labels", []):
            if len(_compact_alnum(label)) >= 6 and label in header_l:
                return vendor["name"]

    # Costco's large stylised logo is commonly read with one wrong character
    # (for example C0STCO), especially on wrinkled thermal paper. It is safe
    # to accept a close match only in the header and only when the nearby
    # WHOLESALE / MEMBER wording confirms the receipt family.
    has_costco_context = any(
        _best_token_match(word, tokens) >= 78
        for word in ("wholesale", "member")
    )
    if has_costco_context and _best_token_match("costco", tokens) >= 72:
        return "Costco"
    return None


def _learned_vendor_match(
    text: str, lines: list[str], learned_vendors: list[dict] | None,
) -> tuple[str | None, bool]:
    """Personal gazetteer first: hard keys, then header-token similarity to saved names."""
    if not learned_vendors:
        return None, False
    phones = _phones_in_text(text)
    store_ids = _store_ids_in_text(text)
    header_key = _header_fingerprint(lines)
    header_zone = "\n".join(_header_lines(lines))

    for vendor in learned_vendors:
        name = (vendor.get("name") or "").strip()
        if not name:
            continue
        if phones & set(vendor.get("phones") or []):
            return name, True
        if store_ids & set(vendor.get("store_ids") or []):
            return name, True
        key = (vendor.get("header_key") or "").strip()
        if key and header_key and key == header_key:
            return name, True

    best: tuple[int, int, str] | None = None
    for vendor in learned_vendors:
        name = (vendor.get("name") or "").strip()
        if not name or len(vendor_name_key(name)) < 3:
            continue
        score = _gazetteer_header_score(name, header_zone)
        if score < _NEAR_EXACT_RATIO:
            continue
        ranked = (score, len(vendor_name_key(name)), name)
        if best is None or ranked > best:
            best = ranked
    if best:
        return best[2], True
    return None, False


def _service_from_context(lines: list[str], blob: str) -> str | None:
    header = "\n".join(_header_lines(lines)).lower()
    header_tokens = set(_word_tokens(header))
    low = (blob or "").lower()
    if "uber eats" in low or ("delivery fee" in low and ("uber" in header_tokens or "eats" in header_tokens)):
        return "Uber Eats"
    if "fare breakdown" in low or "your trip" in low:
        return "Uber"
    if "your lyft" in low or "lyft ride" in low:
        return "Lyft"
    if "tracking number" in low and ("ups" in header_tokens or "ups.com" in low):
        return "UPS"
    if "usps.com" in low or "united states postal" in low:
        return "USPS"
    return None


_GENERIC_HEADER_PHRASES = {
    "receipt", "tax receipt", "cash receipt", "sales receipt", "gift receipt",
    "return receipt", "invoice", "tax invoice", "bill", "order summary",
    "order receipt", "welcome", "thank you", "thank you for your visit",
    "thank you for shopping", "customer copy", "merchant copy", "duplicate",
    "original", "guest check", "order confirmation", "your trip",
    "thanks for riding", "fare breakdown", "tracking number",
}


def _plausible_header_line(line: str) -> bool:
    cleaned = line.strip()
    if not (2 <= len(cleaned) <= 48):
        return False
    letters = sum(c.isalpha() for c in cleaned)
    if letters / max(len(cleaned), 1) < 0.5:
        return False
    if re.search(r"\d{3,}", cleaned):
        return False
    if _ADDRESSY_RE.search(cleaned):
        return False
    if _norm_label_text(cleaned) in _GENERIC_HEADER_PHRASES:
        return False
    return True


def _header_store_candidate(lines: list[str], heights: list[int] | None = None) -> str | None:
    """Prefer the tallest plausible line in the header (logo-sized type)."""
    candidates: list[tuple[int, int, str]] = []
    for i, line in enumerate(_header_lines(lines)):
        if not _plausible_header_line(line):
            continue
        h = heights[i] if heights and i < len(heights) else 0
        candidates.append((h, -i, line.strip()))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][2]


def _find_store_name(
    lines: list[str],
    text: str | None = None,
    *,
    heights: list[int] | None = None,
    learned_vendors: list[dict] | None = None,
) -> tuple[str | None, bool]:
    """Return (name, confident).

    Confident is reserved for hard identifiers: directory phone/store#, or a
    vendor the user previously confirmed for this fingerprint. Header guesses,
    short exact aliases, and near-exact canonicalization are returned for the
    form but do not suppress vision-API escalation.
    """
    blob = text or "\n".join(lines)
    header_zone = "\n".join(_header_lines(lines))

    learned, learned_ok = _learned_vendor_match(blob, lines, learned_vendors)
    if learned:
        return learned, learned_ok

    hard = _hard_vendor_from_directory(blob)
    if hard:
        return hard, True

    header = _header_store_candidate(lines, heights)
    canonical = _canonicalize_from_header(header_zone)
    service = _service_from_context(lines, blob)

    if canonical:
        return canonical, False
    if header:
        return header, False
    if service:
        return service, False
    return None, False


_US_STATE_ZIP = re.compile(
    r"\b(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)\s+\d{5}(?:-\d{4})?\b"
)
_CA_POSTAL = re.compile(r"\b[ABCEGHJ-NPRSTVXY]\d[ABCEGHJ-NPRSTV-Z]\s?\d[ABCEGHJ-NPRSTV-Z]\d\b", re.I)
_UK_POSTAL = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b", re.I)
_INR_MARK = re.compile(r"\b(GSTIN|CGST|SGST|IGST|HSN)\b", re.I)


_CATEGORY_HINTS = (
    ("Food & Dining", ("restaurant", "cafe", "coffee", "pizza", "burger", "diner", "bar")),
    ("Groceries", ("grocery", "supermarket", "market", "trader joe", "whole foods", "walmart", "costco", "publix")),
    ("Transit", ("uber", "lyft", "metro", "transit", "parking", "gas station", "shell", "exxon")),
    ("Travel", ("airline", "airways", "hotel", "airbnb", "booking.com", "expedia")),
    ("Healthcare", ("pharmacy", "clinic", "hospital", "dental", "cvs", "walgreens")),
    ("Utilities", ("electric", "internet", "wifi", "verizon", "at&t", "comcast")),
    ("Entertainment", ("netflix", "spotify", "cinema", "theater", "amc")),
    ("Shopping", ("amazon", "target", "best buy", "ikea", "apple store")),
)


def _guess_category(text: str, store_name: str | None) -> str:
    haystack = f"{store_name or ''} {text}".lower()
    for category, needles in _CATEGORY_HINTS:
        if any(n in haystack for n in needles):
            return category
    return "General"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def _detect_currency(text: str) -> tuple[str, int]:
    """Heuristic ISO currency from receipt text. Confidence 0–2."""
    blob = text or ""
    if _INR_MARK.search(blob) or "₹" in blob or re.search(r"\bRs\.?\s*\d", blob, re.I):
        return "INR", 2
    if "€" in blob or re.search(r"\bEUR\b", blob):
        return "EUR", 2
    if "£" in blob or re.search(r"\bGBP\b", blob):
        return "GBP", 2
    if re.search(r"\bCAD\b|C\$", blob) or _CA_POSTAL.search(blob):
        return "CAD", 1
    if re.search(r"\bAUD\b|A\$", blob):
        return "AUD", 1
    if "$" in blob or re.search(r"\bUSD\b", blob) or _US_STATE_ZIP.search(blob):
        return "USD", 1
    return "USD", 0


def extract_receipt_data_ocr(
    image_bytes: bytes,
    learned_vendors: list[dict] | None = None,
) -> tuple[ReceiptExtraction, bool, dict]:
    """
    Returns (extraction, all_fields_confident, store_fingerprint).

    all_fields_confident=False means at least one field is a low-confidence
    guess (or missing) — the caller can decide to escalate to a VLM if one
    is configured, or just hand the guess to the user to confirm/edit.
    """
    text, mean_conf, boxed_lines, line_lefts, line_heights, line_boxes = _run_ocr(image_bytes)
    lines = boxed_lines or [l for l in text.splitlines() if l.strip()]
    # Drop indented BOGO / $0.00 promo children before total selection.
    lines, line_lefts, line_boxes = _drop_child_promo_lines(lines, line_lefts, line_boxes)
    fingerprint = receipt_store_fingerprint(lines, text)

    store_name, store_confident = _find_store_name(
        lines, text, heights=line_heights, learned_vendors=learned_vendors,
    )
    bill_date, date_confident = _find_date(text)
    total_amount, total_confident = _find_total(lines, line_boxes)
    currency, currency_confidence = _detect_currency(text)
    cached_category = None
    if store_name:
        needle = store_name.strip().lower()
        for vendor in learned_vendors or []:
            if (vendor.get("name") or "").strip().lower() == needle:
                cached_category = (vendor.get("category") or "").strip() or None
                break

    extraction = ReceiptExtraction(
        store_name=store_name or "Unknown store",
        bill_date=bill_date or datetime.now().strftime("%Y-%m-%d"),
        total_amount=total_amount or 0.0,
        currency=currency,
        category=cached_category or _guess_category(text, store_name),
        currency_confidence=currency_confidence,
    )
    fingerprint["guessed_name"] = extraction.store_name
    all_confident = (
        store_confident
        and date_confident
        and total_confident
        and bool(text.strip())
        and mean_conf >= _MIN_TESSERACT_CONF
        and not _looks_implausible(extraction)
    )
    return extraction, all_confident, fingerprint
