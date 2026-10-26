# 前端镜像：Node 构建 → nginx 托管静态资源并反代 /api
# node:20-alpine
FROM m.daocloud.io/docker.io/library/node:20-alpine AS build
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install --registry=https://registry.npmmirror.com
COPY frontend /build
RUN npm run build

# nginx:1.27-alpine
FROM m.daocloud.io/docker.io/library/nginx:1.27-alpine
COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /build/dist /usr/share/nginx/html
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
