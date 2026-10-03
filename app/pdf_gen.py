import base64
import mimetypes
import os
from datetime import datetime
from typing import Optional

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
    group_name: str = "Group",
    base_currency: str = "USD",
    target_currency: Optional[str] = None,
    fx_rate: Optional[float] = None,
) -> tuple[str, str]:
    if not pdf_reports_available():
        raise RuntimeError(
            "PDF reports require WeasyPrint. Please install weasyprint or deploy with Docker."
        )
    from weasyprint import HTML

    logo_path = os.path.join(os.path.dirname(__file__), "static", "logo.png")
    logo_data_uri = _image_to_data_uri(logo_path)

    enriched_expenses = []
    for e in expenses:
        enriched_expenses.append({
            **e,
            "image_data_uri": _image_to_data_uri(e.get("image_path")),
        })

    year, month = period_key.split("-")
    month_label = datetime(int(year), int(month), 1).strftime("%B %Y")

    html_str = _env.get_template("monthly_report.html").render(
        group_name=group_name,
        month_label=month_label,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        balances=balances,
        settlements=settlements,
        expenses=enriched_expenses,
        base_currency=base_currency,
        target_currency=target_currency,
        fx_rate=fx_rate,
        logo_data_uri=logo_data_uri,
    )

    now = datetime.now()
    clean_group = "".join(c for c in group_name if c.isalnum() or c in (" ", "-", "_")).strip()
    filename = f"{clean_group} {now:%m_%d_%Y} Expense Report.pdf"
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


def _money_label(amount: float, currency: str) -> str:
    symbols = {
        "USD": "$",
        "INR": "₹",
        "EUR": "€",
        "GBP": "£",
        "CAD": "CA$",
        "AUD": "A$",
        "JPY": "¥",
        "AED": "AED ",
        "SGD": "S$",
        "CHF": "CHF ",
        "CNY": "¥",
        "NZD": "NZ$",
    }
    prefix = symbols.get((currency or "USD").upper(), f"{currency} ")
    return f"{prefix}{float(amount):,.2f}"


def generate_personal_pdf(
    period_key: str,
    user_name: str,
    expenses: list[dict],
    output_dir: str,
    currency: str = "USD",
) -> tuple[str, str]:
    if not pdf_reports_available():
        raise RuntimeError(
            "PDF reports require WeasyPrint. Please install weasyprint or deploy with Docker."
        )
    from weasyprint import HTML

    logo_path = os.path.join(os.path.dirname(__file__), "static", "logo.png")
    logo_data_uri = _image_to_data_uri(logo_path)

    enriched_expenses = []
    total_spent = 0.0
    category_totals = {}

    for e in expenses:
        amt = float(e.get("converted_amount", e.get("total_amount", 0.0)))
        total_spent += amt
        cat = e.get("category", "General")
        category_totals[cat] = round(category_totals.get(cat, 0.0) + amt, 2)
        orig = float(e.get("total_amount", 0.0))
        orig_curr = e.get("currency") or currency
        enriched_expenses.append({
            **e,
            "amount_label": _money_label(orig, orig_curr),
            "converted_label": _money_label(amt, currency),
            "image_data_uri": _image_to_data_uri(e.get("image_path")),
        })

    year, month = period_key.split("-")
    month_label = datetime(int(year), int(month), 1).strftime("%B %Y")

    html_str = _env.get_template("personal_report.html").render(
        user_name=user_name,
        month_label=month_label,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        total_spent_label=_money_label(total_spent, currency),
        currency=currency,
        category_totals=[
            {
                "name": cat,
                "amount_label": _money_label(amt, currency),
                "pct": round((amt / total_spent * 100) if total_spent else 0, 1),
            }
            for cat, amt in category_totals.items()
        ],
        expenses=enriched_expenses,
        logo_data_uri=logo_data_uri,
    )

    now = datetime.now()
    filename = f"Personal Expense Report {period_key}.pdf"
    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(output_dir, filename)

    HTML(string=html_str, base_url=TEMPLATE_DIR).write_pdf(out_path)
    return out_path, filename


def generate_monthly_digest_pdf(
    period_key: str,
    user_name: str,
    group_sections: list[dict],
    personal_expenses: list[dict],
    private_splits: list[dict],
    output_dir: str,
    revision: int = 1,
) -> tuple[str, str]:
    """Create one account-level monthly report instead of one email per group."""
    if not pdf_reports_available():
        raise RuntimeError("PDF reports require WeasyPrint. Please install weasyprint or deploy with Docker.")
    from weasyprint import HTML

    logo_path = os.path.join(os.path.dirname(__file__), "static", "logo.png")
    year, month = period_key.split("-")
    month_label = datetime(int(year), int(month), 1).strftime("%B %Y")
    html_str = _env.get_template("monthly_digest.html").render(
        user_name=user_name,
        month_label=month_label,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        group_sections=group_sections,
        personal_expenses=personal_expenses,
        private_splits=private_splits,
        revision=revision,
        logo_data_uri=_image_to_data_uri(logo_path),
        money=_money_label,
    )
    clean_user = "".join(c for c in user_name if c.isalnum() or c in (" ", "-", "_")).strip() or "ShareSquad"
    filename = f"{clean_user} {period_key} Monthly Digest.pdf"
    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(output_dir, filename)
    HTML(string=html_str, base_url=TEMPLATE_DIR).write_pdf(out_path)
    return out_path, filename
