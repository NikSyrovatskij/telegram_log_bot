#!/bin/bash
set -e

# ========================================================
# Скрипт автоматической установки и запуска Telegram Log Bot
# Подходит для: Ubuntu 20.04+, Debian 11+
# ========================================================

echo "=================================================="
echo "🚀 Запуск установки Telegram Log Bot на VPS..."
echo "=================================================="

# 1. Проверка прав root
if [ "$EUID" -ne 0 ]; then
  echo "⚠️  Пожалуйста, запустите скрипт с правами root или через sudo:"
  echo "    sudo bash install.sh"
  exit 1
fi

# 2. Обновление пакетов и установка зависимостей
echo "📦 Шаг 1/5: Обновление системы и установка утилит..."
apt-get update -qq && apt-get install -y -qq curl git ufw ca-certificates gnupg > /dev/null 2>&1

# 3. Проверка и установка Docker
echo "🐳 Шаг 2/5: Проверка Docker..."
if ! command -v docker &> /dev/null; then
    echo "   Установка Docker..."
    curl -fsSL https://get.docker.com -o get-docker.sh
    sh get-docker.sh > /dev/null 2>&1
    rm get-docker.sh
    systemctl enable docker
    systemctl start docker
    echo "   ✅ Docker успешно установлен."
else
    echo "   ✅ Docker уже установлен."
fi

# 4. Проверка Docker Compose Plugin
if ! docker compose version &> /dev/null; then
    echo "   Установка Docker Compose Plugin..."
    apt-get update -qq && apt-get install -y -qq docker-compose-plugin > /dev/null 2>&1
fi

# 5. Проверка файла конфигурации .env
echo "⚙️ Шаг 3/5: Проверка файла конфигурации .env..."
if [ ! -f .env ]; then
    if [ -f .env.example ]; then
        cp .env.example .env
        echo "   ⚠️  Создан файл .env из .env.example."
        echo "   ❗️ ВНИМАНИЕ: Отредактируйте .env (укажите BOT_TOKEN, ADMIN_ID и т.д.):"
        echo "      nano .env"
        echo "   Затем повторно запустите: sudo bash install.sh"
        exit 0
    else
        echo "   ❌ Файл .env и .env.example не найдены!"
        exit 1
    fi
fi

# 6. Создание директории для медиа
echo "📁 Шаг 4/5: Подготовка директорий..."
mkdir -p media

# 7. Сборка и запуск контейнеров
echo "🏗 Шаг 5/5: Сборка и запуск контейнеров через Docker Compose..."
docker compose down > /dev/null 2>&1 || true
docker compose up -d --build

echo ""
echo "=================================================="
echo "🎉 БОТ УСПЕШНО УСТАНОВЛЕН И ЗАПУЩЕН!"
echo "=================================================="
echo ""
echo "Статус контейнеров:"
docker compose ps
echo ""
echo "📌 Полезные команды:"
echo " • Просмотр логов:        docker compose logs -f bot"
echo " • Перезапуск:            docker compose restart"
echo " • Остановка:             docker compose down"
echo " • Статус:                docker compose ps"
echo ""

