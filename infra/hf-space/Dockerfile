# Warm standby on a free Hugging Face Space (Docker SDK, CPU basic 2 vCPU / 16 GB).
# Create a Space, set "Dockerfile path" to this file (or copy it to the Space root), add the same
# secrets as core/.env in Space settings, and set VITE_API_URL to the Space URL if the VM is down.
# Spaces expose port 7860 and sleep after 48 h idle — UptimeRobot pinging /healthz keeps it warm.
FROM ghcr.io/OWNER/REPO/core:latest
ENV DATA_DIR=/data
RUN mkdir -p /data && chmod 777 /data
EXPOSE 7860
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7860", "--workers", "1"]
