#!/bin/bash
set -e

# ========================================================
# Скрипт автоматической установки и запуска Telegram Log Bot
# Репозиторий: https://github.com/NikSyrovatskij/telegram_log_bot.git
# Подходит для: Ubuntu 20.04+, Debian 11+
# ========================================================

echo "=================================================="
echo "🚀 Установка Telegram Business Bot на VPS..."
echo "=================================================="

# 1. Проверка прав root
if [ "$EUID" -ne 0 ]; then
  echo "⚠️  Пожалуйста, запустите скрипт с правами root или через sudo:"
  echo "    sudo bash install.sh"
  exit 1
fi

# 2. Обновление пакетов и установка утилит
echo "📦 Шаг 1/5: Обновление системы и установка утилит..."
apt-get update -qq && apt-get install -y -qq curl git ufw ca-certificates gnupg > /dev/null 2>&1

# 3. Клонирование репозитория (если скрипт запущен через curl в пустой папке)
if [ ! -f docker-compose.yml ]; then
    echo "📥 Загрузка файлов проекта из GitHub..."
    git clone https://github.com/NikSyrovatskij/telegram_log_bot.git .
fi

# 4. Проверка и установка Docker
echo "🐳 Шаг 2/5: Проверка Docker..."
if ! command -v docker &> /dev/null; then
    echo "   Установка Docker..."
    curl -fsSL https://get.docker.com -o get-docker.sh
    sh get-docker.sh > /dev/null 2>&1
    rm -f get-docker.sh
    systemctl enable docker
    systemctl start docker
    echo "   ✅ Docker успешно установлен."
else
    echo "   ✅ Docker уже установлен."
fi

# 5. Проверка Docker Compose Plugin
if ! docker compose version &> /dev/null; then
    echo "   Установка Docker Compose Plugin..."
    apt-get update -qq && apt-get install -y -qq docker-compose-plugin > /dev/null 2>&1
fi

# 6. Настройка файла конфигурации .env
echo "⚙️ Шаг 3/5: Настройка файла конфигурации .env..."
if [ ! -f .env ]; then
    if [ -f .env.example ]; then
        cp .env.example .env
        echo "   Создан файл .env из шаблона."
        
        # Интерактивный ввод токена и ID админа (если запущен в терминале)
        if [ -t 0 ]; then
            echo ""
            echo "--------------------------------------------------"
            read -p "👉 Введите BOT_TOKEN (от @BotFather): " input_token
            if [ -n "$input_token" ]; then
                sed -i.bak "s|^BOT_TOKEN=.*|BOT_TOKEN=$input_token|" .env && rm -f .env.bak
            fi
            read -p "👉 Введите ваш ADMIN_ID (Telegram ID): " input_admin_id
            if [ -n "$input_admin_id" ]; then
                sed -i.bak "s|^ADMIN_ID=.*|ADMIN_ID=$input_admin_id|" .env && rm -f .env.bak
            fi
            echo "--------------------------------------------------"
            echo "   ✅ Основные параметры сохранены в .env."
        else
            echo "   ⚠️  Заполните .env (BOT_TOKEN и ADMIN_ID): nano .env"
            echo "   Затем повторно запустите скрипт."
            exit 0
        fi
    else
        echo "   ❌ Файл .env.example не найден!"
        exit 1
    fi
fi

# 7. Создание директории для медиа
echo "📁 Шаг 4/5: Подготовка директорий..."
mkdir -p media

# 8. Сборка и запуск контейнеров
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
