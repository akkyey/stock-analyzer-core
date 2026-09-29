#!/bin/bash
set -euo pipefail

REPO_ROOT="/Users/irom/Documents/stock-analyzer-core"
SCRATCH_ROOT="/Users/irom/.gemini/antigravity/brain/ebfa52ba-a86b-4aa9-9e8e-232d48f8f7fa/scratch"
REMOTE_HOST="masaaki-sv"
REMOTE_DIR="/home/irom/dev/stock-analyzer-core/docs/articles"

echo "=== [1/3] Copying to Gemini Scratch ==="
mkdir -p "${SCRATCH_ROOT}"
mkdir -p "${SCRATCH_ROOT}/docs/articles"
cp "${REPO_ROOT}/docs/articles/qiita_part"*.md "${SCRATCH_ROOT}/"
cp "${REPO_ROOT}/docs/articles/qiita_part"*.md "${SCRATCH_ROOT}/docs/articles/"

echo "=== [2/3] Syncing to Remote Server (${REMOTE_HOST}) ==="
rsync -avz "${REPO_ROOT}/docs/articles/qiita_part"*.md "${REMOTE_HOST}:${REMOTE_DIR}/"

echo "=== [3/3] Verifying MD5 Checksums ==="
MD5_LOCAL_P1=$(md5 -q "${REPO_ROOT}/docs/articles/qiita_part1_data_acquisition.md")
MD5_LOCAL_P2=$(md5 -q "${REPO_ROOT}/docs/articles/qiita_part2_quant_gatekeeper.md")

MD5_SCRATCH_P1=$(md5 -q "${SCRATCH_ROOT}/qiita_part1_data_acquisition.md")
MD5_SCRATCH_P2=$(md5 -q "${SCRATCH_ROOT}/qiita_part2_quant_gatekeeper.md")

MD5_REMOTE=$(ssh "${REMOTE_HOST}" "md5sum ${REMOTE_DIR}/qiita_part*.md")
MD5_REMOTE_P1=$(echo "${MD5_REMOTE}" | grep "qiita_part1" | awk '{print $1}')
MD5_REMOTE_P2=$(echo "${MD5_REMOTE}" | grep "qiita_part2" | awk '{print $1}')

echo "Part 1 (Data Acquisition):"
echo "  Local:   ${MD5_LOCAL_P1}"
echo "  Scratch: ${MD5_SCRATCH_P1}"
echo "  Remote:  ${MD5_REMOTE_P1}"

if [ "${MD5_LOCAL_P1}" = "${MD5_SCRATCH_P1}" ] && [ "${MD5_LOCAL_P1}" = "${MD5_REMOTE_P1}" ]; then
    echo "  => OK (All 3 locations match!)"
else
    echo "  => ERROR (Checksum mismatch in Part 1!)"
    exit 1
fi

echo "Part 2 (Quant Gatekeeper):"
echo "  Local:   ${MD5_LOCAL_P2}"
echo "  Scratch: ${MD5_SCRATCH_P2}"
echo "  Remote:  ${MD5_REMOTE_P2}"

if [ "${MD5_LOCAL_P2}" = "${MD5_SCRATCH_P2}" ] && [ "${MD5_LOCAL_P2}" = "${MD5_REMOTE_P2}" ]; then
    echo "  => OK (All 3 locations match!)"
else
    echo "  => ERROR (Checksum mismatch in Part 2!)"
    exit 1
fi

echo "=== All 3 locations are perfectly in sync! ==="
