from control_plane.dokploy.api import JsonObject, JsonValue


def application_endpoint_spec(application: JsonObject) -> JsonValue:
    return application.get("endpointSpecSwarm") or {"Mode": "dnsrr"}
