# syntax=docker/dockerfile:1

# ---- deps: install exactly what the lock file says ---------------------------
FROM node:22-alpine AS deps
WORKDIR /app
RUN chown node:node /app
USER node
COPY --chown=node:node package.json package-lock.json ./
RUN --mount=type=cache,target=/home/node/.npm,uid=1000,gid=1000 \
    npm ci --no-audit --no-fund

# ---- dev: Vite dev server with hot reload ------------------------------------
FROM deps AS dev
# Source is bind-mounted by Docker Compose; copied here so the image also works alone.
COPY --chown=node:node . .
EXPOSE 5173
CMD ["npm", "run", "dev"]

# ---- build: type-check and produce static assets -----------------------------
FROM deps AS build
COPY --chown=node:node . .
ARG VITE_API_BASE_URL=/api/v1
ARG VITE_APP_NAME="QA Agent"
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL} VITE_APP_NAME=${VITE_APP_NAME}
RUN npm run build

# ---- prod: static files served by unprivileged Nginx -------------------------
FROM nginxinc/nginx-unprivileged:1.27-alpine AS prod
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
