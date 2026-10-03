from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from kraken_ensemble_to_text import _canonical_speaker, page_text  # noqa: E402


def alto(*, header: str, speaker: str, body: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<alto>
  <Layout>
    <Page WIDTH="1000" HEIGHT="1000">
      <PrintSpace>
        <TextBlock>
          <TextLine HPOS="450" VPOS="50" WIDTH="100" HEIGHT="40">
            <String CONTENT="{header}" />
          </TextLine>
          <TextLine HPOS="800" VPOS="300" WIDTH="100" HEIGHT="50">
            <String CONTENT="{speaker}" />
          </TextLine>
          <TextLine HPOS="100" VPOS="300" WIDTH="650" HEIGHT="50">
            <String CONTENT="{body}" />
          </TextLine>
        </TextBlock>
      </PrintSpace>
    </Page>
  </Layout>
</alto>
"""


class EnsembleTests(unittest.TestCase):
    def test_normalizes_frequent_speaker_confusions(self) -> None:
        self.assertEqual(_canonical_speaker("يیحیی"), "يحيى")
        self.assertEqual(_canonical_speaker("هشیر"), "منير")
        self.assertEqual(_canonical_speaker("یسیس"), "بسبس")

    def test_uses_fallback_speaker_and_primary_body(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            primary = root / "primary.xml"
            fallback = root / "fallback.xml"
            primary.write_text(
                alto(
                    header="--",
                    speaker="حى",
                    body="الهدف النهائى بلغة الكتابة",
                ),
                encoding="utf-8",
            )
            fallback.write_text(
                alto(
                    header="١٨١",
                    speaker="يیحیی",
                    body="الهدف النهانى يلغة الكتاية",
                ),
                encoding="utf-8",
            )

            result = page_text(primary, fallback)

        self.assertEqual(result, "يحيى: الهدف النهائى بلغة الكتابة")


if __name__ == "__main__":
    unittest.main()
