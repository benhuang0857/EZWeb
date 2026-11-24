start:
	docker-compose up -d --build

healthcheck:
	docker-compose run --rm healthcheck

down:
	docker-compose down

install: start healthcheck

configure:
	docker-compose -f docker-compose.yml -f wp-auto-config.yml run --rm wp-auto-config

autoinstall: start
	docker-compose -f docker-compose.yml -f wp-auto-config.yml run --rm wp-auto-config

wp:
	docker exec $(COMPOSE_PROJECT_NAME)_wpcli wp $(filter-out $@,$(MAKECMDGOALS))

plugin-install:
	docker exec $(COMPOSE_PROJECT_NAME)_wpcli wp plugin install $(PLUGIN) --activate

plugin-list:
	docker exec $(COMPOSE_PROJECT_NAME)_wpcli wp plugin list

theme-install:
	docker exec $(COMPOSE_PROJECT_NAME)_wpcli wp theme install $(THEME) --activate

clean: down
	@echo "💥 Removing related folders/files..."
	@rm -rf  mysql/* wordpress/*

reset: clean

