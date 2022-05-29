run: build
	docker run -t -p 8080:8080 binance-challenge

build:
	docker build -t binance-challenge .

