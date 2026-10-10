"""Inert, database-backed public website and writer adapter for isolated proof."""

import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import FastAPI

from control_plane.contracts.prepared_public_site import (
    WRITER_KINDS,
    CapturedPublicResponse,
    PreparedPublicSite,
    PublicRecoveryObservation,
    PublicSiteBinding,
    PublicSitePlan,
    WriterObservation,
    WriterTarget,
)
from control_plane.prepared_public_pause import PreparedPublicPause
from control_plane.prepared_public_site import prepare_public_site, prepared_public_app
from control_plane.prepared_public_storage import FilePublicPauseStore


class WebsiteFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.binding = PublicSiteBinding(
            product_id="fixture-site",
            environment_id="isolated",
            release_id="fixture-release",
            artifact_id="fixture-artifact",
            image_digest="sha256:" + "a" * 64,
            database="fixture-database",
        )
        self.plan = PublicSitePlan(
            binding=self.binding,
            origin="https://fixture.invalid",
            public_routes=("/", "/contact", "/prices/repair", "/old-prices"),
            retained_assets=("/assets/old.css",),
            excluded_prefixes=("/my", "/web", "/metrics", "/editor"),
            writers=tuple(WriterTarget(writer_id=kind, kind=kind) for kind in sorted(WRITER_KINDS)),
            notice="We are updating the site. Browsing is available; forms and signed-in activity are temporarily paused.",
        )
        self.database = root / "website.sqlite"
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("CREATE TABLE content (value TEXT)")
            connection.execute("INSERT INTO content VALUES ('Representative repair prices')")
            connection.execute("CREATE TABLE writes (writer TEXT)")
        self.assets = root / "assets"
        self.assets.mkdir()
        (self.assets / "old.css").write_text(".legacy {color: purple}")
        self.pages: dict[str, tuple[str, bytes]] = {
            "/assets/site.css": (
                "text/css",
                b"body {margin:0} .hero {background-image:url('/assets/bg.png')}",
            ),
            "/assets/old.css": ("text/css", (self.assets / "old.css").read_bytes()),
            "/assets/bg.png": (
                "image/png",
                bytes.fromhex(
                    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000b49444154789c636000020000050001a5f645400000000049454e44ae426082"
                ),
            ),
            "/assets/site.js": (
                "text/javascript",
                b"fetch('/metrics', {method:'POST'}); document.cookie='tracking=1';",
            ),
        }
        self.capture_paths: list[str] = []
        self.fenced: set[str] = set()
        self.active: dict[str, int] = {writer.writer_id: 0 for writer in self.plan.writers}
        self.published = ""
        self.resume_count = 0
        self.fail_resume = False
        self.bad_publication = False
        self.owner_count = 1
        self.recovered = False

    def capture(self, path: str) -> CapturedPublicResponse:
        self.capture_paths.append(path)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            content = str(connection.execute("SELECT value FROM content").fetchone()[0])
        headers = {
            "Set-Cookie": "session_id=never-retain",
            "Content-Language": "en",
            "X-CSRF-Token": "never-retain",
        }
        if path == "/old-prices":
            return CapturedPublicResponse(
                binding=self.binding,
                anonymous=True,
                public=True,
                status=302,
                content_type="text/html",
                headers={"Location": "/prices/repair"},
            )
        if path in self.plan.public_routes:
            body = (
                '<html><head><title>Repair shop</title><link rel="stylesheet" href="/assets/site.css">'
                '<script src="/assets/site.js"></script></head><body>'
                '<header><a href="/">Home</a> <a href="/contact">Contact</a> '
                '<a href="/prices/repair">Repair prices</a> <a href="/old-prices">Old prices</a>'
                ' <a href="/my">Account</a></header>'
                f'<main><h1>{content}</h1><img src="/assets/bg.png" alt="Repair icon">'
                '<form action="/contact" method="post"><input name="csrf_token" value="secret-csrf">'
                '<input name="message"><button>Send</button></form>'
                '<script>var csrf_token="secret-csrf"; fetch("/metrics");</script>'
                "</main></body></html>"
            ).encode()
            return CapturedPublicResponse(
                binding=self.binding,
                anonymous=True,
                public=True,
                status=200,
                content_type="text/html",
                body=body,
                headers=headers,
            )
        content_type, body = self.pages.get(path, ("text/plain", b"missing"))
        return CapturedPublicResponse(
            binding=self.binding,
            anonymous=True,
            public=True,
            status=200 if path in self.pages else 404,
            content_type=content_type,
            body=body,
            headers=headers,
        )

    def write(self, writer_id: str) -> bool:
        if writer_id in self.fenced:
            return False
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("INSERT INTO writes VALUES (?)", (writer_id,))
        return True

    def publish(self, site: PreparedPublicSite, pause_id: str) -> str:
        self.published = "wrong" if self.bad_publication else site.content_digest
        return self.published

    def observed_public_copy(self, pause_id: str) -> str:
        return self.published

    def fence_and_drain(
        self, site: PreparedPublicSite, pause_id: str
    ) -> tuple[WriterObservation, ...]:
        self.fenced = {writer.writer_id for writer in site.plan.writers}
        return tuple(
            WriterObservation(
                binding=site.plan.binding,
                pause_id=pause_id,
                writer=writer,
                fenced=True,
                active_jobs=self.active[writer.writer_id],
                evidence_id=f"fixture-fence-{writer.writer_id}",
            )
            for writer in site.plan.writers
        )

    def recover_and_resume(
        self, site: PreparedPublicSite, pause_id: str, expected: PublicSiteBinding
    ) -> PublicRecoveryObservation:
        if self.fail_resume:
            raise RuntimeError("fixture recovery unavailable")
        self.resume_count += 1
        self.fenced.clear()
        self.recovered = True
        return PublicRecoveryObservation(
            binding=expected,
            pause_id=pause_id,
            serving_evidence_id="fixture-verified-serving",
            writer_owners=tuple(
                (writer.writer_id, self.owner_count) for writer in site.plan.writers
            ),
        )

    def update_database(self) -> None:
        if self.fenced != {writer.writer_id for writer in self.plan.writers}:
            raise RuntimeError("migration without complete fence")
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("DROP TABLE content")
            connection.execute("CREATE TABLE content (value TEXT, new_column TEXT)")
            connection.execute("INSERT INTO content VALUES ('Updated repair prices', 'migration')")
        (self.assets / "old.css").unlink()
        self.pages.pop("/assets/old.css")


def browser_app() -> FastAPI:
    """Task-owned localhost fixture, held in the database-changing pause state."""
    temporary = TemporaryDirectory(prefix="prepared-public-browser-")
    fixture = WebsiteFixture(Path(temporary.name))
    site = prepare_public_site(fixture.plan, fixture)
    pause = PreparedPublicPause(site, FilePublicPauseStore(fixture.root / "state"), fixture)
    pause.begin()
    fixture.update_database()
    # Even losing the authoritative DB after migration cannot affect prepared serving.
    fixture.database.unlink()
    app = prepared_public_app(site)
    app.state.fixture_temporary = temporary
    return app
