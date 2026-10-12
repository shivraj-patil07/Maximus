# MAXIMUS Backend — extends AIDA's backend image with Pro Tools API
FROM vasco0x4/aida-backend:latest

WORKDIR /app

# Copy MAXIMUS additions
COPY api/pro_tools.py /app/api/pro_tools.py
COPY maximus_main.py /app/maximus_main.py

# MAXIMUS branding — override AIDA report templates
COPY report_service.py /app/services/report_service.py
COPY report_templates/report.html.j2 /app/services/report_templates/report.html.j2
COPY report_templates/report.css /app/services/report_templates/report.css

# Use MAXIMUS's main (extends AIDA's app with /api/v1/pro/* routes)
CMD ["uvicorn", "maximus_main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--log-level", "info", "--proxy-headers", "--forwarded-allow-ips", "*"]
