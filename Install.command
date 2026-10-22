#!/bin/bash
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
exec /bin/bash "$root/Install.sh" "$@"
