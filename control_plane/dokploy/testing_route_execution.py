"""Original-attempt exclusion in the managed proxy's existing route mount."""

from typing import Literal

from pydantic import Field

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.prepared_public_site import FrozenRecord


class TestingRouteExecutionCustody(FrozenRecord):
    mount_source: str = Field(min_length=1)
    filesystem: str = Field(pattern=r"^[0-9]+:[0-9]+$")
    proxy_id: str = ""
    proxy_started_at: str = ""
    process_epoch: str = ""

    @property
    def boot_id(self) -> str:
        if not self.process_epoch:
            raise ValueError("Route execution requires its original boot identity.")
        return self.process_epoch.split(":", 1)[0]

    @property
    def directory_sha256(self) -> str:
        return canonical_json_sha256({"filesystem": self.filesystem})

    @property
    def physical_sha256(self) -> str:
        if not self.proxy_id or not self.proxy_started_at or not self.process_epoch:
            raise ValueError("Physical route execution requires its original proxy identity.")
        return canonical_json_sha256(
            {
                "filesystem": self.filesystem,
                "proxy_id": self.proxy_id,
                "proxy_started_at": self.proxy_started_at,
                "process_epoch": self.process_epoch,
            }
        )


class TestingRouteExecutionIntent(FrozenRecord):
    operation_id: str = Field(min_length=1)
    server_id: str = Field(min_length=1)
    route_id: str = Field(pattern=r"^lp-testing-route-[a-f0-9]{32}$")
    route_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    backend: str | None
    document_sha256: str = Field(pattern=r"^(absent|[a-f0-9]{64})$")
    custody: TestingRouteExecutionCustody

    def key(self, *, attempt: int) -> str:
        if attempt < 1:
            raise ValueError("A route execution requires its original positive attempt.")
        return canonical_json_sha256({"intent": self.model_dump(mode="json"), "attempt": attempt})


class TestingRouteFence(FrozenRecord):
    generation: str = Field(pattern=r"^[a-f0-9]{64}$")
    lock_identity: str = Field(pattern=r"^[0-9]+:[0-9]+$")
    revision: int = Field(ge=0)
    key: str = Field(pattern=r"^(none|[a-f0-9]{64})$")
    desired: str = Field(pattern=r"^(absent|[a-f0-9]{64})$")
    original: str = Field(pattern=r"^(absent|sealed|[a-f0-9]{64})$")
    state: Literal["initialized", "pending", "applied", "excluded"]


class TestingRouteLockIntent(FrozenRecord):
    server_id: str = Field(min_length=1)
    route_id: str = Field(pattern=r"^lp-testing-route-[a-f0-9]{32}$")
    custody: TestingRouteExecutionCustody

    @property
    def key(self) -> str:
        return canonical_json_sha256(
            {
                "server_id": self.server_id,
                "route_id": self.route_id,
                "directory": self.custody.directory_sha256,
                "boot_id": self.custody.boot_id,
            }
        )


class TestingRouteInitializer(FrozenRecord):
    operation_id: str = Field(min_length=1)
    server_id: str = Field(min_length=1)
    route_id: str = Field(pattern=r"^lp-testing-route-[a-f0-9]{32}$")
    custody: TestingRouteExecutionCustody
    generation: str = Field(pattern=r"^[a-f0-9]{64}$")
    previous: TestingRouteFence | None = None
    # Bound before initialization dispatch, including the first generation.
    # Losing this lock never permits same-root creation of a replacement.
    lock_identity: str = Field(pattern=r"^[0-9]+:[0-9]+$")
    retired_boot_id: str = ""


class TestingRouteWriterAttempt(FrozenRecord):
    execution: TestingRouteExecutionIntent
    custody: TestingRouteExecutionCustody
    fence: TestingRouteFence
    expected_file_sha256: str = Field(pattern=r"^(absent|[a-f0-9]{64})$")
    key: str = Field(pattern=r"^[a-f0-9]{64}$")


def parse_testing_route_fence(content: str) -> dict[str, object]:
    """Decode the fixed read-only shell frame, also emitted on the host."""
    lines = content.splitlines()
    if lines in (["missing"], ["busy"]):
        return {"status": lines[0], "lock_identity": "", "fence": None}
    if len(lines) == 2 and lines[0] == "partial":
        return {"status": "partial", "lock_identity": lines[1], "fence": None}
    status, _, actual_lock = lines[0].partition(":")
    if len(lines) != 8 or status not in {"ready", "rebound"} or not lines[3].isdigit():
        raise ValueError("The managed route fence is incomplete.")
    fields = ("generation", "lock_identity", "revision", "key", "desired", "original", "state")
    fence: dict[str, object] = dict(zip(fields, lines[1:], strict=True))
    fence["revision"] = int(lines[3])
    return {
        "status": status,
        "lock_identity": actual_lock if status == "rebound" else lines[2],
        "fence": fence,
    }


VERIFY_TESTING_ROUTE_PROCESS = r"""
boot=$(cat /proc/sys/kernel/random/boot_id)
process=$(cat /proc/1/stat)
process=${process##*) }
set -- $process
shift 19
started=$1
offset='boottime 0 0'
if test -e /proc/self/timens_offsets; then
    offset=$(sed -n '/^boottime/p' /proc/self/timens_offsets)
fi
set -- $offset
test "$boot:$started:$2:$3" = "$process_epoch"
"""


ALLOCATE_TESTING_ROUTE_LOCK = (
    r"""
set -eu
root=$1
name=$2
filesystem=$3
process_epoch=$4
"""
    + VERIFY_TESTING_ROUTE_PROCESS
    + r"""
case "$name" in lp-testing-route-*.yml) ;; *) exit 21 ;; esac
test ! -L "$root"
cd -P -- "$root"
test "$(stat -c '%d:%i' .)" = "$filesystem"
lock=".$name.lock"
test ! -L "$lock"
umask 077
if test ! -e "$lock"; then
    test ! -e ".$name.receipt"
    test ! -e ".$name.executions"
    (set -C; : > "$lock")
fi
test -f "$lock"
exec 9<"$lock"
if ! flock -n 9; then printf 'busy\n'; exit 0; fi
actual_lock=$(stat -Lc '%d:%i' /proc/self/fd/9)
test "$(stat -c '%d:%i' "$lock")" = "$actual_lock"
sync -f .
printf '%s\n' "$actual_lock"
"""
)


READ_TESTING_ROUTE_FENCE = r"""
set -eu
root=$1
name=$2
filesystem=$3
case "$name" in lp-testing-route-*.yml) ;; *) exit 21 ;; esac
test ! -L "$root"
cd -P -- "$root"
test "$(stat -c '%d:%i' .)" = "$filesystem"
lock=".$name.lock"
receipt=".$name.receipt"
test ! -L "$lock"
test ! -L "$receipt"
if test ! -e "$lock"; then
    test ! -e "$receipt"
    printf 'missing\n'
    exit 0
fi
test -f "$lock"
exec 9<"$lock"
if ! flock -n 9; then printf 'busy\n'; exit 0; fi
actual_lock=$(stat -Lc '%d:%i' /proc/self/fd/9)
test "$(stat -c '%d:%i' "$lock")" = "$actual_lock"
if test ! -e "$receipt"; then
    printf 'partial\n%s\n' "$actual_lock"
    exit 0
fi
test -f "$receipt"
test "$(wc -c < "$receipt")" -lt 1024
    if test "$(sed -n '2p' "$receipt")" = "$actual_lock"; then printf 'ready\n'
    else printf 'rebound:%s\n' "$actual_lock"; fi
cat "$receipt"
"""


# Initialization is a separate, exactly-once durable child effect. Only its
# original nonce, observed under its original lock, can complete that effect.
# Ordinary calls never initialize or recreate missing fencing evidence.
FENCED_TESTING_ROUTE_WRITER = (
    r"""
set -eu
root=$1
name=$2
filesystem=$3
mode=$4
generation=$5
revision=$6
key=$7
expected=$8
desired=$9
shift 9
prior_generation=$1
lock_identity=$2
process_epoch=$3
prior_lock_identity=${4:-$lock_identity}
retired_boot_id=${5:-}
"""
    + VERIFY_TESTING_ROUTE_PROCESS
    + r"""
case "$name" in lp-testing-route-*.yml) ;; *) exit 21 ;; esac
case "$mode" in initialize|inspect|publish|settle) ;; *) exit 21 ;; esac
case "$generation$key" in *[!a-f0-9]*) exit 21 ;; esac
test "${#generation}" = 64
test "${#key}" = 64
if test "$desired" != absent; then
    case "$desired" in *[!a-f0-9]*) exit 21 ;; esac
    test "${#desired}" = 64
fi
case "$revision" in ''|*[!0-9]*) exit 21 ;; esac
test ! -L "$root"
cd -P -- "$root"
test "$(stat -c '%d:%i' .)" = "$filesystem"
lock=".$name.lock"
receipt=".$name.receipt"
path="$name"
umask 077
test ! -L "$lock"
test ! -L "$receipt"
# Allocation is creation-only and has its own durable resource receipt. Every
# generation invocation is bound to the resulting lock BEFORE dispatch.
test "$lock_identity" != absent
test -f "$lock"
exec 9<"$lock"
if ! flock -n 9; then printf 'busy\n'; exit 0; fi
actual_lock=$(stat -Lc '%d:%i' /proc/self/fd/9)
if test "$lock_identity" != absent; then test "$actual_lock" = "$lock_identity"; fi
test "$(stat -c '%d:%i' "$lock")" = "$actual_lock"
temporary=
trap 'test -z "$temporary" || rm -f "$temporary"' EXIT HUP INT TERM

digest() {
    test ! -L "$path"
    if test -e "$path"; then
        test -f "$path"
        sha256sum "$path" | cut -d ' ' -f 1
    else
        printf 'absent\n'
    fi
}

save() {
    temporary=$(mktemp ".$name.pending.XXXXXX")
    printf '%s\n' "$generation" "$actual_lock" "$current_revision" "$current_key" "$current_desired" "$original" "$state" > "$temporary"
    sync -f "$temporary"
    mv -f "$temporary" "$receipt"
    temporary=
    sync -f .
}

clean_staging() {
    # Every live staging user inherits descriptor 9. Exclusive custody proves
    # that no remaining child can still consume an abandoned file.
    for leftover in ".$name".pending.*; do
        test -e "$leftover" || continue
        test ! -L "$leftover"
        rm -f "$leftover"
    done
}

if test -e "$receipt"; then
    test -f "$receipt"
    test "$(wc -c < "$receipt")" -lt 1024
    {
        IFS= read -r saved_generation
        IFS= read -r saved_lock
        IFS= read -r current_revision
        IFS= read -r current_key
        IFS= read -r current_desired
        IFS= read -r original
        IFS= read -r state
        if IFS= read -r unexpected; then exit 23; fi
    } < "$receipt"
    if test "$saved_lock" != "$actual_lock"; then
        test "$mode" = initialize
        test "$saved_lock" = "$prior_lock_identity"
        test -n "$retired_boot_id"
        test "$boot" != "$retired_boot_id"
    fi
    case "$current_revision" in ''|*[!0-9]*) exit 23 ;; esac
    case "$state" in initialized|pending|applied|excluded) ;; *) exit 23 ;; esac
else
    test "$mode" = initialize
    test "$prior_generation" = absent
    saved_generation=absent
    current_revision=0
fi

if test "$mode" = initialize; then
    if test "$saved_generation" = "$generation"; then
        # Read-only adoption of this one original initializer's witness.
        cat "$receipt"
        exit 0
    fi
    if test "$saved_generation" != "$prior_generation" || test "$current_revision" != "$revision"; then
        printf 'not-started\n'
        exit 0
    fi
    current_revision=0
    current_key=none
    current_desired=$(digest)
    original=$current_desired
    state=initialized
    clean_staging
    save
    cat "$receipt"
    exit 0
fi

test "$saved_generation" = "$generation"
if test "$mode" = inspect; then cat "$receipt"; exit 0; fi

clean_staging

if test "$current_key" = "$key"; then
    test "$current_desired" = "$desired"
    if test "$state" = pending; then
        current=$(digest)
        if test "$current" = "$desired"; then state=applied
        elif test "$current" = "$original"; then state=excluded
        else printf 'unknown\n'; exit 0
        fi
        save
    fi
    case "$state" in applied|excluded) printf '%s\n' "$state"; exit 0 ;; *) exit 23 ;; esac
fi
if test "$current_revision" != "$revision"; then
    printf 'superseded\n'
    exit 0
fi
current_revision=$((current_revision + 1))
current_key=$key
current_desired=$desired
original=$expected
if test "$mode" = settle; then
    original=sealed
    state=excluded
    save
    printf 'excluded\n'
    exit 0
fi
current=$(digest)
if test "$current" != "$expected"; then
    state=excluded
    save
    printf 'excluded\n'
    exit 0
fi
state=pending
save
if test "$desired" = absent; then
    test "$(digest)" = "$expected"
    test ! -L "$path"
    if test -e "$path"; then rm -- "$path"; fi
    sync -f .
    state=applied
    save
    printf 'applied\n'
    exit 0
fi
temporary=$(mktemp ".$name.pending.XXXXXX")
cat > "$temporary"
test "$(sha256sum "$temporary" | cut -d ' ' -f 1)" = "$desired"
sync -f "$temporary"
test "$(digest)" = "$expected"
mv -f "$temporary" "$path"
temporary=
sync -f .
state=applied
save
printf 'applied\n'
"""
)
