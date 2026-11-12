# Use the official lightweight Python image
FROM python:3.11-slim-bookworm

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Set the working directory
WORKDIR /app

# Copy the application code directly (no requirements.txt needed!)
COPY ./app ./app

# Expose the port (Render will override this dynamically)
EXPOSE 8000

# Run the application
CMD ["python", "app/main.py"]