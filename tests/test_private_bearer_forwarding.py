import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from control_plane.npmplus import PRIVATE_BEARER_FORWARDING


@unittest.skipUnless(
    os.environ.get("LAUNCHPLANE_TEST_NGINX_IMAGE"), "Select an immutable local Nginx image"
)
class PrivateBearerForwardingTests(unittest.TestCase):
    def test_existing_access_rules_and_basic_stripping_survive_bearer_forwarding(self) -> None:
        image = os.environ["LAUNCHPLANE_TEST_NGINX_IMAGE"]
        if not (image.startswith("sha256:") or "@sha256:" in image):
            raise ValueError("The native rehearsal requires an immutable image")
        access = (
            'auth_basic "synthetic private";\n'
            "auth_basic_user_file /tmp/empty-users;\n"
            "allow 127.0.0.1; deny all; satisfy any;\n"
        )
        clear = 'proxy_set_header Authorization "";\n'
        servers = []
        for port, directives in (
            (8080, clear + PRIVATE_BEARER_FORWARDING),
            (8082, PRIVATE_BEARER_FORWARDING + clear),
        ):
            servers.append(
                f"server {{ listen {port}; location / {{ {access}{directives}"
                "proxy_pass http://127.0.0.1:8081; } }"
            )
        servers.append(
            'server { listen 8083; location / { auth_basic "synthetic private"; '
            "auth_basic_user_file /tmp/empty-users; deny all; satisfy any; "
            + clear
            + PRIVATE_BEARER_FORWARDING
            + "proxy_pass http://127.0.0.1:8081; } }"
        )
        config = (
            "events {}\nhttp { access_log off; error_log /dev/stderr crit;\n"
            'server { listen 8081; location / { return 200 "seen:$http_authorization"; } }\n'
            + "\n".join(servers)
            + "\n}"
        )
        commands = ["touch /tmp/empty-users", "nginx -t -q", "nginx"]
        expected = []
        for port in (8080, 8082):
            for header, upstream in (
                ("Bearer synthetic", "seen:Bearer synthetic"),
                ("Basic c3ludGhldGljOnRlc3Q=", "seen:"),
            ):
                commands.append(
                    f"wget -qO- --header 'Authorization: {header}' http://127.0.0.1:{port}/agent-session/connect"
                )
                commands.append("printf '\\n'")
                expected.append(upstream)
        commands.extend(
            [
                "if wget -qO- --header 'Authorization: Bearer synthetic' http://127.0.0.1:8083/agent-session/connect; "
                "then exit 7; else printf 'denied\\n'; fi",
                "nginx -s quit",
            ]
        )
        expected.append("denied")
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "nginx.conf"
            path.write_text(config)
            result = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--pull=never",
                    "--network=none",
                    "--entrypoint",
                    "sh",
                    "--mount",
                    f"type=bind,source={path.resolve()},target=/etc/nginx/nginx.conf,readonly",
                    image,
                    "-ec",
                    "\n".join(commands),
                ],
                text=True,
                capture_output=True,
                timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), expected)
