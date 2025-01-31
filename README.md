# WayPointDB

## Overview
WayPointDB is a lightweight GPS data collection API that interacts with a PostgreSQL database and is deployed using Docker and Nginx. WayPointDB is focused on fast and efficient data collection and retrieval, so the user wont have to wait around for the webpage to load or the data to be processed.

## Features
- Multi-user support
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

## Configuration
The project can be configured by modifying variables in the ```docker-compose.yml``` file. The following variables are commonly modified:
- <strong>backend: environment: ```PHOTON_SERVER_*```</strong>
The host, HTTPS status, and an optional API key of the Photon server
- <strong>nginx: ports: ```80:80```</strong>
The first port is the port at which WayPointDB is accessible, and can be customized to an available port on the host machine.

## Usage

### First Steps
The project can be accessed by navigating to the IP address of the host machine on the specified port. Log in with the default credentials: ```admin@example.com``` and ```password```. The E-Mail and password can be changed in the ```Account``` page and is strongly recommended.

### API
An API key for each user can be generated in the respective ```Account``` page, and is required for API requests.

WayPointDB has a Swagger API documentation page that can be accessed by navigating to ```/api/v1/docs```.

### Data Collection
There are currently two ways to collect GPS data using a mobile device:
- Using the Overland app, which can be configured to send data to WayPointDB using the ```/api/v1/gps/overland``` API endpoint
- Using the [WayPointDB iOS App](https://github.com/yniverz/WayPointDB-iOS) which however has to be built and installed yourself as it is not available on the App Store

### Users
Users can be added, edited, and deleted in the ```Manage Users``` page. Only ```admin``` users can view this page.


## Update
To update the project, pull the latest changes from the repository and rebuild the Docker image
```bash
docker-compose down
git pull
docker-compose build
docker-compose up -d
```

## Photon Server
WayPointDB can use a [Photon server](https://github.com/komoot/photon) for reverse geocoding. It is recommended to use a self-hosted instance of the Photon server to avoid rate limiting, and to ensure the privacy of the data. Some hosting providers use an api key for authentication, which can be set in the ```docker-compose.yml``` file. WayPointDB will pass this as the ```X-Api-Key``` header in the requests to the Photon server.

<hr>

### References
- icon made by [Freepik](https://www.flaticon.com/authors/freepik)