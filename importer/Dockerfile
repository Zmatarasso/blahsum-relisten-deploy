FROM python:3.12-slim

# ffprobe for accurate audio durations + bitrate
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY blahsum_importer ./blahsum_importer

ENTRYPOINT ["python", "-m", "blahsum_importer"]
CMD ["import"]
