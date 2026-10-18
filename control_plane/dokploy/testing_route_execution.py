"""Original-attempt exclusion in the managed proxy's existing route mount."""

from typing import Literal

from pydantic import Field

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.prepared_public_site import FrozenRecord

TestingRouteExecutionOutcome = Literal["applied", "excluded", "busy", "unknown"]
ROUTE_RECEIPT_PHASE = "testing-route-receipt-v1:"


class TestingRouteExecutionCustody(FrozenRecord):
    mount_source: str = Field(min_length=1)
    filesystem: str = Field(pattern=r"^[0-9]+:[0-9]+$")


class TestingRouteExecutionIntent(FrozenRecord):
    operation_id: str = Field(min_length=1)
    server_id: str = Field(min_length=1)
    route_id: str = Field(pattern=r"^lp-testing-route-[a-f0-9]{32}$")
    route_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    backend: str = Field(min_length=1)
    document_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    custody: TestingRouteExecutionCustody

    def key(self, *, attempt: int) -> str:
        if attempt < 1:
            raise ValueError("A route execution requires its original positive attempt.")
        return canonical_json_sha256({"intent": self.model_dump(mode="json"), "attempt": attempt})


# The open lock descriptor is inherited by cat/mv/sync. Killing their shell
# cannot release exclusion while a surviving child could still rename a route.
# A settled seal also rejects an original Docker exec that has not arrived yet.
ATOMIC_TESTING_ROUTE_WRITER = r"""
set -eu
root=$1
name=$2
expected=$3
desired=$4
key=$5
filesystem=$6
custody=$7
mode=$8
case "$name" in lp-testing-route-*.yml) ;; *) exit 21 ;; esac
case "$mode" in publish|settle) ;; *) exit 21 ;; esac
case "$key$desired$custody" in *[!a-f0-9]*) exit 21 ;; esac
test "${#key}" = 64
test "${#desired}" = 64
test "${#custody}" = 64
test -d "$root"
test ! -L "$root"
test "$(stat -c '%d:%i' "$root")" = "$filesystem"
path="$root/$name"
directory="$root/.$name.executions"
umask 077
test ! -L "$directory"
mkdir -p "$directory"
sync -f "$root"
lock="$directory/$key.lock"
receipt="$directory/$key.receipt"
test ! -L "$lock"
exec 9>>"$lock"
if ! flock -n 9; then printf 'busy\n'; exit 0; fi
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
    temporary=$(mktemp "$directory/.pending.XXXXXX")
    printf '%s\n' "$key" "$desired" "$custody" "$original" "$1" > "$temporary"
    sync -f "$temporary"
    mv -f "$temporary" "$receipt"
    temporary=
    sync -f "$directory"
}

test ! -L "$receipt"
if test -e "$receipt"; then
    test -f "$receipt"
    test "$(wc -c < "$receipt")" -lt 1024
    {
        IFS= read -r prior_key
        IFS= read -r prior_desired
        IFS= read -r prior_custody
        IFS= read -r original
        IFS= read -r state
        if IFS= read -r unexpected; then exit 23; fi
    } < "$receipt"
    test "$prior_key" = "$key"
    test "$prior_desired" = "$desired"
    test "$prior_custody" = "$custody"
    case "$state" in
        applied|excluded) printf '%s\n' "$state"; exit 0 ;;
        pending) ;;
        *) exit 23 ;;
    esac
    current=$(digest)
    if test "$current" = "$desired"; then state=applied
    elif test "$current" = "$original"; then state=excluded
    else printf 'unknown\n'; exit 0
    fi
    save "$state"
    printf '%s\n' "$state"
    exit 0
fi

original=$expected
if test "$mode" = settle; then
    original=sealed
    save excluded
    printf 'excluded\n'
    exit 0
fi
current=$(digest)
if test "$current" = "$desired"; then
    save applied
    printf 'applied\n'
    exit 0
fi
if test "$current" != "$expected"; then
    save excluded
    printf 'excluded\n'
    exit 0
fi
save pending
temporary=$(mktemp "$root/.launchplane-testing.pending.XXXXXX")
cat > "$temporary"
test "$(sha256sum "$temporary" | cut -d ' ' -f 1)" = "$desired"
sync -f "$temporary"
test "$(digest)" = "$expected"
mv -f "$temporary" "$path"
temporary=
sync -f "$root"
save applied
printf 'applied\n'
"""
