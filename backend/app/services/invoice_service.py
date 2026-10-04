"""In-memory PDF invoice generator using ReportLab."""

import io
from datetime import datetime
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.lib import colors


def generate_invoice_pdf(
    customer_name: str,
    invoice_number: str,
    amount: int = 500,
) -> bytes:
    """Generate a clean Moraa Studio receipt/invoice in memory."""
    buffer = io.BytesIO()
    p = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter

    # Header
    p.setFont("Helvetica-Bold", 20)
    p.setFillColor(colors.HexColor("#1A1A1A"))
    p.drawString(50, height - 70, "Moraa Studio")

    p.setFont("Helvetica", 10)
    p.setFillColor(colors.HexColor("#666666"))
    p.drawString(50, height - 88, "AI Jewelry Transformation Platform")

    # Divider line
    p.setStrokeColor(colors.HexColor("#DDDDDD"))
    p.setLineWidth(1)
    p.line(50, height - 105, width - 50, height - 105)

    # Invoice Details
    p.setFont("Helvetica-Bold", 12)
    p.setFillColor(colors.HexColor("#1A1A1A"))
    p.drawString(50, height - 140, "PAYMENT RECEIPT")

    p.setFont("Helvetica", 10)
    p.drawString(50, height - 165, f"Receipt No: {invoice_number}")
    p.drawString(50, height - 180, f"Date: {datetime.now().strftime('%d %b %Y, %I:%M %p')}")
    p.drawString(50, height - 195, f"Billed To: {customer_name}")

    # Item Table Box
    table_top = height - 230
    p.setFillColor(colors.HexColor("#F8F9FA"))
    p.rect(50, table_top - 30, width - 100, 30, fill=1, stroke=0)

    p.setFillColor(colors.HexColor("#1A1A1A"))
    p.setFont("Helvetica-Bold", 10)
    p.drawString(60, table_top - 20, "Description")
    p.drawRightString(width - 60, table_top - 20, "Amount (INR)")

    p.setFont("Helvetica", 10)
    p.drawString(60, table_top - 55, "Wallet Credit Recharge (Moraa AI Transformation)")
    p.drawRightString(width - 60, table_top - 55, f"₹{amount}.00")

    p.setStrokeColor(colors.HexColor("#DDDDDD"))
    p.line(50, table_top - 70, width - 50, table_top - 70)

    p.setFont("Helvetica-Bold", 11)
    p.drawString(60, table_top - 95, "Total Paid:")
    p.drawRightString(width - 60, table_top - 95, f"₹{amount}.00")

    # Footer note
    p.setFont("Helvetica-Oblique", 9)
    p.setFillColor(colors.HexColor("#777777"))
    p.drawString(50, 75, "This is a payment receipt, not a GST tax invoice. A tax invoice is issued separately where applicable.")
    p.drawString(50, 60, "Thank you for creating with Moraa Studio! Questions? Contact support@moraa.studio")

    p.showPage()
    p.save()

    buffer.seek(0)
    return buffer.getvalue()