"""Download the small Apache-2.0 Vosk model once; never called at bot runtime."""
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path

MODEL_NAME = "vosk-model-small-en-us-0.15"
URL = f"https://alphacephei.com/vosk/models/{MODEL_NAME}.zip"
MODEL_PATH = Path(__file__).resolve().parent / "models" / MODEL_NAME


def main():
    if (MODEL_PATH / "am" / "final.mdl").is_file():
        print("Carter wake model is already installed.")
        return
    if MODEL_PATH.exists():
        raise RuntimeError("Wake model directory is incomplete; choose a fresh model directory.")
    MODEL_PATH.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".wake-download-", dir=MODEL_PATH.parent) as temporary:
        temporary = Path(temporary)
        archive = temporary / "model.zip"
        with urllib.request.urlopen(URL, timeout=60) as response, archive.open("wb") as output:
            total = 0
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > 64 * 1024 * 1024:
                    raise RuntimeError("Wake model download exceeded its size limit")
                output.write(chunk)
        with zipfile.ZipFile(archive) as zipped:
            if sum(member.file_size for member in zipped.infolist()) > 256 * 1024 * 1024:
                raise RuntimeError("Wake model archive exceeded its unpacked size limit")
            for member in zipped.infolist():
                target = (temporary / member.filename).resolve()
                if not target.is_relative_to(temporary / MODEL_NAME):
                    raise RuntimeError("Unexpected path in wake model archive")
                if (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise RuntimeError("Unexpected symlink in wake model archive")
            zipped.extractall(temporary)
        extracted = temporary / MODEL_NAME
        if not (extracted / "am" / "final.mdl").is_file():
            raise RuntimeError("Downloaded model is incomplete")
        shutil.move(str(extracted), str(MODEL_PATH))
    print("Installed Carter's local wake model. No API key is required for wake detection.")


if __name__ == "__main__":
    main()
