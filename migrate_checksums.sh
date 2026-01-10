#!/bin/bash
BUCKET="$1"
shift
OTHER_FILES=("$@")
if [ -e "$BUCKET.sha256.old" ] || [ -e "$BUCKET.sha256sum.old" ]; then
    echo "Error: 'Old' checksum file exists. Exiting script." >&2
    exit 1
fi
mv "$BUCKET.sha256" "$BUCKET.sha256.old"
mv "$BUCKET.sha256sum" "$BUCKET.sha256sum.old"
~/objectindex/scripts/gen_cksum_from_filename.py "$BUCKET" || exit 1
cat "$BUCKET.sha256sum"  "$BUCKET.sha256.old"  "$BUCKET.sha256sum.old" "${OTHER_FILES[@]}" | sort | uniq > "$BUCKET.sha256"
cd "$BUCKET" || exit 1
sha256sum -c "../$BUCKET.sha256" || exit 1
