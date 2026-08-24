# ProcureHub frontend: Vite build -> static files served by nginx.
#
# IMPORTANT: Vite inlines VITE_* variables at BUILD time, not at run time.
# The API URL therefore has to be a build argument -- changing it later means
# rebuilding this image, not just restarting the container.

FROM node:22-alpine AS build

WORKDIR /app

# package.json + lockfile first so dependencies are cached independently of
# source changes. `npm ci` when a lockfile exists (reproducible), otherwise
# fall back to `npm install` so the build works on a fresh clone too.
COPY package*.json ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

COPY . .

# Where the browser should reach the backend. Default matches the compose
# setup (backend published on the host at :8000). For a real deployment pass
# the public API URL:
#   docker build --build-arg VITE_API_BASE_URL=https://api.example.com .
ARG VITE_API_BASE_URL=http://localhost:8000
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
RUN npm run build


FROM nginx:1.27-alpine AS runtime

# SPA routing: React Router owns /command-centre, /vendor-inventory, ... so
# nginx must serve index.html for any path that is not a real file.
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -q --spider http://localhost/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
