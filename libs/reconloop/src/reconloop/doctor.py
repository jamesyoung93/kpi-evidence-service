import importlib
from importlib.metadata import version
from io import BytesIO
from pathlib import Path
import platform
import sys
from uuid import uuid4


def _sample_pdf():
    stream = b"BT /F1 32 Tf 25 65 Td (RECON 12345) Tj ET"
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 320 120] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
               b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"]
    data, offsets = b"%PDF-1.4\n", [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(data)
    data += b"xref\n0 6\n0000000000 65535 f \n"
    data += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    return data + f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()


def diagnose(*, ocr=False, path=None, write_probe=False):
    if write_probe and path is None:
        raise ValueError("write_probe requires a storage path")
    checks = []

    def check(name, operation, fix):
        try:
            detail = operation()
            checks.append(dict(name=name, ok=True, detail=str(detail)))
        except Exception as exc:
            checks.append(dict(name=name, ok=False, detail=str(exc), remedy=fix))

    check("python", lambda: platform.python_version() if sys.version_info >= (3, 11) else
          (_ for _ in ()).throw(RuntimeError("Python >=3.11 required")), "Use a runtime with Python 3.11 or newer.")
    for module, distribution in [("pdfplumber", "pdfplumber"), ("pypdfium2", "pypdfium2"), ("PIL", "Pillow")]:
        def installed(m=module, d=distribution):
            importlib.import_module(m)
            return version(d)
        check(distribution, installed, "Install the reconloop PDF/OCR extras in this Python environment.")

    def render():
        import pdfplumber
        with pdfplumber.open(BytesIO(_sample_pdf())) as pdf:
            with pdf.pages[0].to_image(resolution=150).original as image:
                if ocr:
                    from .extract.pdf import tesseract_read
                    text = tesseract_read(image, timeout=20).text
                    if "12345" not in text.replace(" ", ""):
                        raise RuntimeError(f"render/OCR smoke test could not read the known digits: {text!r}")
                    return "PDF rendered and known digits extracted"
                return f"PDF rendered: {image.width}x{image.height}"

    if ocr:
        def engine():
            import pytesseract
            found = pytesseract.get_tesseract_version()
            if "eng" not in pytesseract.get_languages(config=""):
                raise RuntimeError("Tesseract English language data is missing")
            return str(found)
        check("tesseract", engine, "Install the pinned OCR system packages on classic compute, with eng language data and PATH configured.")
    check("render_and_ocr" if ocr else "pdf_render", render,
          "Check PDFium and, for OCR, the Tesseract executable and language data in this process.")
    if path is not None:
        def storage():
            from .storage import local_path
            directory = local_path(path)
            if not directory.is_dir():
                raise FileNotFoundError("storage path must be an existing accessible directory")
            next(directory.iterdir(), None)
            if write_probe:
                probe = directory / ("reconloop-probe-" + uuid4().hex + ".txt")
                try:
                    with probe.open("x", encoding="utf-8") as stream:
                        stream.write("reconloop storage probe")
                    if probe.read_text(encoding="utf-8") != "reconloop storage probe":
                        raise RuntimeError("storage probe readback failed")
                finally:
                    probe.unlink(missing_ok=True)
            return "read/write probe passed" if write_probe else "directory is readable (writes not tested)"
        check("storage", storage, "Verify the runtime, mounted volume, catalog/schema usage, and read/write volume privileges.")
    return dict(ok=all(c["ok"] for c in checks), checks=checks)
