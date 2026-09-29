FROM mcr.microsoft.com/dotnet/sdk:8.0

RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pip && rm -rf /var/lib/apt/lists/*
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1 PYTHONUNBUFFERED=1 HF_HOME=/app/.cache/hf FINDAJEV_ROOT=/app
WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt fastapi "uvicorn[standard]"

COPY src ./src
RUN dotnet build src/FindAJev.Bench -c Release -o bin -v q

COPY models.json server.py PROJECT.md ./
COPY tools ./tools
COPY results ./results

# Spaces run the container as uid 1000
RUN useradd -m -u 1000 user && mkdir -p /app/models /app/data /app/.cache && chown -R 1000:1000 /app
USER user
EXPOSE 7860
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "7860"]
