FROM node:14-alpine

WORKDIR /app

COPY package.json ./
RUN npm install -g serve

COPY index.html ./

EXPOSE 8080

CMD ["serve", "-s", ".", "-l", "8080"]
