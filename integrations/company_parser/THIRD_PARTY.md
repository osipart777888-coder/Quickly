# Сторонний код

`vendor/parser_2gis/` — исходный код [interlark/parser-2gis](https://github.com/interlark/parser-2gis) от Andy Trofimov, commit `e12cc050ea811ed76e65a1be938214b6583f9072` (21 апреля 2024 года).

Лицензия — GNU Lesser General Public License v3.0 or later (LGPL-3.0-or-later). Копия лицензии находится в `vendor/LICENSE.parser-2gis`. Исходный README — `vendor/README.parser-2gis.md`, данные происхождения — `vendor/UPSTREAM.json`. Код этого каталога не изменён; используется непосредственно из исходников, которые можно заменить на совместимую версию.

Дополнения в `company_emails/`, тесты и сценарии запуска распространяются по MIT (`LICENSE`). Они используют браузерный движок upstream, каталог городов и рубрикатор. Ответственность за стабильность страниц 2ГИС и полноту каталога не возлагается на авторов upstream.

`data/yandex-cities.json` содержит производную выборку из [GeoNames](https://www.geonames.org/), архив [cities15000.zip](https://download.geonames.org/export/dump/cities15000.zip) от 27 октября 2026 года. Лицензия данных — [Creative Commons Attribution 4.0](https://creativecommons.org/licenses/by/4.0/), указанная в [официальном описании формата](https://download.geonames.org/export/dump/readme.txt). Изменения: фильтр `country=RU`, исключение кварталов `PPLX`, выбор русскоязычных имён, добавление кодов программы, сортировка. Дата получения и исходный URL сохранены в metadata. Источник не гарантирует точность и полноту; это географический справочник, а не база компаний. Подробнее — `data/README.md`.
# World geography

Country names and populated places come from [GeoNames](https://www.geonames.org/export/),
licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
Quickly downloads `countryInfo.txt` and `cities500.zip` from the official dump server
into a local SQLite cache. Source date and SHA-256 are recorded in its metadata.
The populated-place catalogue includes places over 500 inhabitants and administrative
seats; it does not certify complete coverage of companies or public email addresses.
