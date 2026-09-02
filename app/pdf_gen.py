import base64
import mimetypes
import os
from datetime import datetime

from jinja2 import Environment, FileSystemLoader

TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")
_env = Environment(loader=FileSystemLoader(TEMPLATE_DIR))

_PDF_AVAILABLE: bool | None = None


def pdf_reports_available() -> bool:
    global _PDF_AVAILABLE
    if _PDF_AVAILABLE is None:
        try:
            from weasyprint import HTML  # noqa: F401
            _PDF_AVAILABLE = True
        except (ImportError, OSError):
            _PDF_AVAILABLE = False
    return _PDF_AVAILABLE


def _image_to_data_uri(path: str) -> str | None:
    if not path or not os.path.exists(path):
        return None
    mime, _ = mimetypes.guess_type(path)
    mime = mime or "image/jpeg"
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def generate_monthly_pdf(
    period_key: str,
    balances: list[dict],
    settlements: list[dict],
    expenses: list[dict],
    output_dir: str,
) -> tuple[str, str]:
    if not pdf_reports_available():
        raise RuntimeError(
            "PDF reports are not available on this hosting plan. "
            "Deploy with Docker on AWS for full PDF support."
        )
    from weasyprint import HTML

    enriched_expenses = []
    for e in expenses:
        enriched_expenses.append({
            **e,
            "image_data_uri": _image_to_data_uri(e.get("image_path")),
        })

    year, month = period_key.split("-")
    month_label = datetime(int(year), int(month), 1).strftime("%B %Y")

    html_str = _env.get_template("monthly_report.html").render(
        month_label=month_label,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        balances=balances,
        settlements=settlements,
        expenses=enriched_expenses,
    )

    now = datetime.now()
    filename = f"{now:%m_%d_%Y} Expense Calculated.pdf"
    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(output_dir, filename)

    counter = 2
    base_name = filename[:-4]
    while os.path.exists(out_path):
        filename = f"{base_name} ({counter}).pdf"
        out_path = os.path.join(output_dir, filename)
        counter += 1

    HTML(string=html_str, base_url=TEMPLATE_DIR).write_pdf(out_path)
    return out_path, filename
