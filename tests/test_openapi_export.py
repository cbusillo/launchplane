import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest import TestCase
from unittest.mock import patch

from control_plane.openapi_export import write_canonical_openapi


class OpenApiExportTests(TestCase):
    def test_concurrent_readers_always_receive_complete_schema(self) -> None:
        payload = {"description": "schema" * 200_000}
        with TemporaryDirectory() as directory:
            output = Path(directory) / "schema.json"
            output.write_text(json.dumps(payload))
            finished = Event()

            def regenerate() -> None:
                try:
                    for _ in range(25):
                        write_canonical_openapi(output)
                finally:
                    finished.set()

            with patch(
                "control_plane.openapi_export.canonical_openapi_document", return_value=payload
            ):
                with ThreadPoolExecutor(max_workers=1) as executor:
                    writer = executor.submit(regenerate)
                    while not finished.is_set():
                        self.assertEqual(json.loads(output.read_text()), payload)
                    writer.result()
            self.assertEqual(json.loads(output.read_text()), payload)
