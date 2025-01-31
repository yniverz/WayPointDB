# WayPointDB

## Overview
WayPointDB is a lightweight GPS data collection API that interacts with a PostgreSQL database and is deployed using Docker and Nginx. WayPointDB is focused on fast and efficient data collection and retrieval, so the user wont have to wait around for the webpage to load or the data to be processed.

## Features
- REST API for GPS data collection
- Pure Flask-based interface
- PostgreSQL as the database backend
- Reverse proxy using Nginx
- Dockerized deployment

## Prerequisites
Before running the project, ensure you have the following installed:
- [Docker](https://www.docker.com/)
- [Docker Compose](https://docs.docker.com/compose/)

## Installation
To install the project, clone the repository, change directory to the project folder, build the Docker image, and run the Docker container (```-d``` for detached mode)
```bash
git clone https://github.com/yniverz/WayPointDB
cd WayPointDB
docker-compose build
docker-compose up -d
```

### Update
To update the project, pull the latest changes from the repository and rebuild the Docker image
```bash
docker-compose down
git pull
docker-compose build
docker-compose up -d
```

## Configuration
The project can be configured by modifying the environment variables in the ```docker-compose.yml``` file. The following environment variables are commonly modified:
- ```OUTBOUND_PORT``` in the NginX section: The port on which the Nginx server listens
- ```PHOTON_SERVER_*``` in the backend section: The host, HTTPS status, and an optional API key of the Photon server

### Photon Server
WayPointDB can use a [Photon server](https://github.com/komoot/photon) for reverse geocoding. It is recommended to use a self-hosted instance of the Photon server to avoid rate limiting, and to ensure the privacy of the data. Some hosting providers use an api key for authentication, which can be set in the ```docker-compose.yml``` file. WayPointDB will pass this as the ```X-Api-Key``` header in the requests to the Photon server.