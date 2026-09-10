"""Shared transaction-lock identities for mutable merge-admission evidence."""


def change_impact(repository_id: str) -> str:
    return f"change-impact:{repository_id}"


def engineering_authority(repository: str) -> str:
    return f"engineering-review-authority:{repository.strip().lower()}"


def engineering_decision(repository: str, pull_request_number: int) -> str:
    return f"engineering-review-decision:{repository.strip().lower()}:{pull_request_number}"


def owner_acceptance(repository_id: str, pull_request_number: int) -> str:
    return f"owner-acceptance:{repository_id}:{pull_request_number}"


def product_owner(table_name: str, product: str, system: str) -> str:
    return f"product-owner:{table_name}:{product}:{system}"


def product_profile(product: str) -> str:
    return f"product-profile:{product}"


def preview_anchor(repository: str, pull_request_number: int) -> str:
    # Legacy preview rows may use a bare repository name. Both representations
    # must contend; equal basenames conservatively share the same narrow lock.
    name = repository.strip().lower().rsplit("/", 1)[-1]
    return f"preview-anchor:{name}:{pull_request_number}"


def preview_identity(preview_id: str) -> str:
    return f"preview-identity:{preview_id}"


def preview_generation_identity(generation_id: str) -> str:
    return f"preview-generation-identity:{generation_id}"
