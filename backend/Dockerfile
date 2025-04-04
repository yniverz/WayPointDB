# Use official Python image
FROM python:3.13

# Set working directory
WORKDIR /app

# Copy project files
COPY backend/requirements.txt requirements.txt

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the app
COPY backend/. .

# Copy VERSION file from the parent directory
COPY VERSION ./VERSION

# Expose Flask port
EXPOSE 8500

# Run Waitress
CMD ["python", "-u", "app.py"]