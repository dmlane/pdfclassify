"""Manipulate the name and/or metadata of a PDF file."""

import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from pypdf.errors import PdfReadError

from pdfclassify._util import CONFIG, MyException
from pdfclassify.argument_handler import ParsedArgs
from pdfclassify.pdf_metadata_manager import PDFMetadataManager
from pdfclassify.pdf_semantic_classifier import Classification, PDFSemanticClassifier


class PdfProcess:
    """Process a PDF file with metadata saving and classification."""

    def __init__(self, pdf_path: str):
        self.pdf_file = Path(pdf_path)
        if not self.pdf_file.is_file():
            raise MyException(f"File {pdf_path} does not exist", 1)

        # ── skip zero-length iCloud placeholder PDFs ──
        try:
            size = self.pdf_file.stat().st_size
            try:
                xattrs = os.listxattr(self.pdf_file)
            except (AttributeError, NotImplementedError):
                raw = subprocess.check_output(
                    ["xattr", str(self.pdf_file)], stderr=subprocess.DEVNULL
                )
                xattrs = raw.decode("utf-8", errors="ignore").splitlines()
            if size == 0 and "com.apple.placeholder" in xattrs:
                raise MyException(f"Skipping zero-length placeholder PDF: {pdf_path}", 4)
        except OSError:
            # ignore stat errors
            pass

        # Save original metadata
        try:
            self._save_metadata()
        except PdfReadError as e:
            raise MyException(f"Invalid PDF file: {e}", 2) from e

    def display_info(self) -> None:
        """Display PDF metadata."""
        PDFMetadataManager(self.pdf_file).print_metadata()

    def _save_metadata(self) -> None:
        """Write /Original_Filename and /Original_Date if not already present."""
        try:
            pdf_manager = PDFMetadataManager(self.pdf_file)
            mod_date = datetime.fromtimestamp(self.pdf_file.stat().st_mtime).isoformat()
            if pdf_manager.read_custom_field("/Original_Filename") is None:
                pdf_manager.write_custom_field("/Original_Filename", self.pdf_file.name)
            if pdf_manager.read_custom_field("/Original_Date") is None:
                pdf_manager.write_custom_field("/Original_Date", mod_date)
        except PdfReadError as e:
            raise MyException(f"Invalid PDF file: {e}", 2) from e
        except Exception as e:
            raise MyException(f"Unexpected error during metadata save: {e}", 3) from e

    def restore_original_state(self) -> None:
        """Restore filename and timestamp from metadata fields."""
        pdf_manager = PDFMetadataManager(self.pdf_file)
        orig_name = pdf_manager.read_custom_field("/Original_Filename")
        orig_date = pdf_manager.read_custom_field("/Original_Date")
        if orig_name:
            new_path = self.pdf_file.with_name(Path(orig_name).name)
            self.pdf_file.rename(new_path)
            self.pdf_file = new_path
        if orig_date:
            ts = datetime.fromisoformat(orig_date).timestamp()
            os.utime(self.pdf_file, (ts, ts))

    def predict(self, args: ParsedArgs) -> None:
        """Predict the label using trained classifier; queue empty-text PDFs for OCR."""
        pdf_manager = PDFMetadataManager(self.pdf_file)
        classifier = PDFSemanticClassifier(data_dir=args.training_data_path)
        classifier.train()
        try:
            label = classifier.predict(
                pdf_path=str(self.pdf_file),
                confidence_threshold=CONFIG.confidence_threshold,
            )
        except ValueError as err:
            if "empty" in str(err).lower():
                print(f"⚠️ Skipping '{self.pdf_file.name}': no text extracted. Queuing for 2OCR.")
                queue_dir = self.pdf_file.parent / "pdfclassify.2ocr"
                queue_dir.mkdir(parents=True, exist_ok=True)
                dest = queue_dir / self.pdf_file.name
                counter = 1
                while dest.exists():
                    dest = queue_dir / f"{self.pdf_file.stem}_{counter}.pdf"
                    counter += 1
                shutil.move(str(self.pdf_file), dest)
                print(f"File moved to: {dest}")
                return
            raise
        # Regular processing
        print(f"Predicted label: {label.label} with confidence {label.confidence:.2f}")
        if label.success:
            pdf_manager.write_custom_field(
                field_name="/Classification", value=label.label, overwrite=True
            )
        new_path = self.take_action(
            file_name=self.pdf_file,
            prediction=label,
            rename=not args.no_rename,
            output_path=Path(args.output_path),
        )
        if new_path:
            print(f"File moved to: {new_path}")

    def take_action(
        self,
        file_name: Path,
        prediction: Classification,
        rename: bool = True,
        output_path: Path = None,
    ) -> Path | None:
        """Rename/move file based on classification success or rejects."""
        if not rename:
            return None
        suffix = ".pdf"
        if prediction.success:
            base = output_path or file_name.parent
            stem = prediction.label
        else:
            base = file_name.parent / "pdfclassify.rejects"
            stem = file_name.stem
        base.mkdir(parents=True, exist_ok=True)
        dest = base / f"{stem}{suffix}"
        counter = 1
        while dest.exists():
            dest = base / f"{stem}_{counter}{suffix}"
            counter += 1
        shutil.move(str(file_name), dest)
        return dest
