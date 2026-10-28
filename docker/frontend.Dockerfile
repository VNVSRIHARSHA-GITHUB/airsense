# AirSense web pages (Stage 8): static files served by nginx, which also
# forwards /api/... to the backend so the browser talks to one address only.
# Build from the project root:
#   docker build -f docker/frontend.Dockerfile -t airsense-frontend:$(git rev-parse --short HEAD) .
FROM nginxinc/nginx-unprivileged:1.27-alpine
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY frontend /usr/share/nginx/html
# In the container the API is on the same address under /api (see nginx.conf)
COPY docker/config.js /usr/share/nginx/html/assets/config.js
EXPOSE 8080
