# Use an official Python runtime as a parent image
FROM python:3.10-slim

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file into the container at /app
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code into the container at /app
COPY . .

# Make port 8080 available to the world outside this container
# Cloud Run will automatically use the PORT environment variable
EXPOSE 8080

# Define environment variable for the port
ENV PORT 8080

# Run app.py when the container launches using Gunicorn
# Gunicorn is a production-ready web server.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "app:app"]