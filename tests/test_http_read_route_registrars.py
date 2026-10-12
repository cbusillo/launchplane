import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from control_plane.openapi_export import write_canonical_openapi


class FastApiReadRouteRegistrarTests(unittest.TestCase):
    def test_canonical_openapi_bytes_match_checked_contract(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            output_path = Path(temporary_directory_name) / "openapi-canonical.json"
            write_canonical_openapi(output_path)

            self.assertEqual(
                output_path.read_bytes(),
                Path("frontend/generated/openapi-canonical.json").read_bytes(),
            )


if __name__ == "__main__":
    unittest.main()
