# Stage 1: build the Cedar FFI library (Rust). cedar-policy is pinned to 4.13.0 in Cargo.toml / Cargo.lock.
FROM rust:1-slim-bookworm AS cedar
WORKDIR /w
COPY third_party/CedarDotNet/src/CedarDotNetFfi ./ffi
RUN cd ffi && cargo build --release

# Stage 2: harness (.NET 10, needed because CedarDotNet multi-targets net8/9/10) + Python API server
FROM mcr.microsoft.com/dotnet/sdk:10.0

RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pip && rm -rf /var/lib/apt/lists/*
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1 PYTHONUNBUFFERED=1 HF_HOME=/app/.cache/hf FINDAJEV_ROOT=/app
WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt fastapi "uvicorn[standard]"

COPY third_party ./third_party
COPY --from=cedar /w/ffi/target/release/libcedar_dotnet_ffi.so ./third_party/CedarDotNet/src/CedarDotNet/runtimes/linux-x64/native/
COPY src ./src
RUN dotnet build src/FindAJev.Bench -c Release -o bin -v q

COPY policies ./policies
COPY models.json server.py dashboard.html PROJECT.md ./
COPY tools ./tools
COPY results ./results
# Fail the image build if the policies do not validate or a golden case regresses.
RUN dotnet bin/FindAJev.Bench.dll policy-test

# Spaces run the container as uid 1000; the .NET 10 base image already has a user with that uid, so use it numerically
ENV HOME=/app
RUN mkdir -p /app/models /app/data /app/.cache && chown -R 1000:1000 /app
USER 1000
EXPOSE 7860
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "7860"]
