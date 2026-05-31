# Phase-3 sandbox image — researchflow-analyst:0.2
#
# Built once per release, NEVER at runtime. The container is invoked with:
#   docker run --rm --network=none --read-only --cap-drop=ALL ... \
#       --memory=512m --cpus=1 --user 65534:65534 \
#       -v <workdir>:/work:rw researchflow-analyst:0.2 python /work/run.py
#
# See app/services/sandbox.py::docker_argv for the exact flag set.
#
# Build:
#     docker build -t researchflow-analyst:0.2 backend/sandbox
#
# This image MUST NOT contain any tool that opens a socket (curl, wget,
# git, ssh, etc) — the runtime --network=none flag is a hard block, but
# the image stays minimal as defense in depth.
FROM python:3.11-alpine

# pip is the only writer the install needs; the runtime uses --read-only
# so we install everything at build time as root, then drop perms.
RUN pip install --no-cache-dir \
    "numpy==2.1.2" \
    "pandas==2.2.3" \
    "matplotlib==3.9.2" \
    "scipy==1.14.1" \
    "scikit-learn==1.5.2"

# Run as the nobody user (65534:65534).  The harness ALSO passes
# --user=65534:65534 so this only matters if someone forgets the flag;
# defense in depth.
USER 65534:65534

# /work is bind-mounted RW by the harness; everything else stays RO at
# runtime via --read-only.
WORKDIR /work

# No entrypoint — the harness passes `python /work/run.py` explicitly.
