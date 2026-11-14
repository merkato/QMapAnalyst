# QMapAnalyst 0.3.0 – biblioteka punktów referencyjnych

Gałąź: `feature/v0.3.0`. Koncepcja pełna (biblioteka + serie): 13.11.2026.
Zakres 0.3.0: biblioteka punktów, zestawy regionalne, jakość punktu dla wskazanej mapy.
Tryb serii – później (0.3.x / 0.4), liczony na żądanie.

## Ustalenia

1. **Bez historii obliczeń.** Nie przechowujemy `runs` ani `residuals` – wyniki przelicza się z danych
   wejściowych (położenie na skanie, status, punkt biblioteki). Seria w przyszłości przelicza wszystkie
   mapy jednym modelem na żądanie. Ewentualne „zamrożenie wyniku do raportu” – osobna, jawna akcja.
2. **Jeden plik biblioteki użytkownika** (GeoPackage), ścieżka w ustawieniach QGIS, wspólna dla wszystkich
   projektów; można przełączyć na inny plik. Układ współrzędnych wybierany przy zakładaniu
   (domyślnie układ projektu); przy użyciu przeliczany do układu odniesienia analizy.
3. **Współrzędne w analizie: kopia + ostrzeżenie.** Przy powiązaniu GCP z punktem biblioteki współrzędne
   są kopiowane. Gdy punkt w bibliotece się zmieni, wtyczka pokazuje różnicę i pyta o aktualizację.
4. **Wagi a priori – opcja, domyślnie wyłączona.** Bez niej wyniki jak w 0.2.x; status jest informacją
   i filtrem. Po włączeniu waga = f(status) · f(niepewność współrzędnych z biblioteki).
   Propozycja: pewny 1, niepewny 0,25; nieczytelny i brak na mapie – poza obliczeniami.
5. **Statusy punktu na mapie:** pewny / niepewny / nieczytelny / brak na mapie + komentarz.
   „Brak na mapie” zapisywany bez położenia na skanie – informacja (np. do macierzy pokrycia), nie błąd.
6. **Dwie jakości rozdzielone:** niepewność współrzędnych odniesienia (`ref_points.sigma_m`, cecha obiektu)
   i jakość identyfikacji na danej mapie (`gcp_links.status`).
7. **Zestawy zamiast tagów:** `point_sets` + `set_members` (punkt w wielu zestawach).
8. **Biblioteka jako zwykła warstwa** – edycję, tabelę atrybutów i przyciąganie zapewnia QGIS;
   wtyczka zakłada schemat, styl, wyszukiwarkę i powiązanie z GCP.

## Schemat (wersja 2)

| Tabela | Pola | Klucz |
|---|---|---|
| ref_points (geometria punktowa) | uid, name, alt_names, type, feature, sigma_m, valid_from, valid_to, source, note | uid |
| point_sets | set_id, name, description | set_id |
| set_members | set_id, uid | set_id + uid |
| maps (od wersji 2) | map_id, title, author, year_from, year_to, scale, note | map_id |
| analyses | analysis_id, map_id, scan_path, scan_sha256, title, author, year_from, year_to, scale, sheet, dpi, ref_crs | analysis_id |
| gcp_links | analysis_id, seq, uid, name, x_old, y_old, ref_x, ref_y, enabled, status, comment | analysis_id + uid (gdy uid niepusty) |
| qma_meta | key, value (wersja schematu) | key |

- `gcp_links.ref_x/ref_y` – kopia współrzędnych w układzie analizy (ustalenie 3).
- `gcp_links.uid` może być pusty – punkt spoza biblioteki (np. szczegół widoczny tylko na tej mapie);
  `seq` zachowuje numerację punktów, `name` – kopię nazwy.
- Kody statusów i typów w pliku są stałe (ASCII), nazwy wyświetlane tłumaczy interfejs:
  statusy `sure` / `uncertain` / `illegible` / `absent`; typy `town`, `village`, `church`, `monastery`,
  `castle`, `manor`, `mill`, `bridge`, `confluence`, `crossroads`, `peak`, `control`, `other`.
- Usunięcie punktu użytego w analizach wymaga potwierdzenia; powiązania zostają jako punkty spoza biblioteki.

Stan:
- etap 0 i 1 – `library.py`, `tests/test_library.py`;
- etap 2 – zakładka Biblioteka (`library_panel.py`, `tests/library_qgis.py`): plik w ustawieniach,
  warstwa ze stylem wg typu i formularzem (automatyczny uid, lista typów), filtr po zestawie,
  zestawy z zaznaczenia (przeniesione z etapu 5), wyróżnienie punktów użytych (`GCP.uid`, zmienna warstwy
  `qma_used_uids`). Połączenie OGR otwierane tylko na czas operacji.
- dodatkowo: przycisk „Do biblioteki” w zakładce GCP – zaznaczone GCP trafiają do biblioteki i wybranego
  zestawu (punkt do 1 m od istniejącego jest wiązany); niezapisane edycje warstwy i punkty bez uid
  obsługiwane przed operacjami na zestawach.
- etap 3 – przyciąganie w kroku 2/2 (10 px, widoczna warstwa z filtrem), wyszukiwarka w zakładce GCP
  (nazwy dawne, ranking wg odległości od położenia przewidywanego), „Następny z zestawu” z położeniem
  przewidywanym na skanie (transformacja odwrotna) i pomijaniem, ochrona przed dublowaniem uid
  (`tests/library_pick_qgis.py`).
- etap 4 – kolumny Status (pewny / niepewny / nieczytelny; nieczytelny poza obliczeniami) i Uwagi w tabeli GCP,
  „Brak na mapie” dla punktów zestawu, zapis i odczyt analizy w bibliotece (`analysis_store.py`), opis mapy,
  SHA-256 skanu, podpowiedź o analizie tego skanu po jego wczytaniu, ostrzeżenie o punktach poprawionych lub
  usuniętych w bibliotece (`tests/library_analysis_qgis.py`).
- etap 4b – wagi a priori: `fit_transform(..., weights)` (ważone MNK; σ₀ jednostkowe, kowariancja,
  liczby nadwyżkowe i test Baardy z H = A N⁻¹ Aᵀ P, LOO (I − H_ii)⁻¹ v_i, AICc z vᵀPv; reszty i RMSE
  geometryczne), `apriori_weights` = czynnik statusu · σ²ₘ/(σ²ₘ + σ²ᵣ), σₘ z wyrównania z samymi
  czynnikami statusu; `library.STATUS_WEIGHTS` (pewny 1, niepewny 0,25); przełącznik w zakładce Analiza
  (domyślnie wył.), waga w podpowiedzi kolumny w, wiersz raportu. Testy: równania normalne, LOO = ponowne
  wyrównanie, niezmienniczość skali wag. Estymacja odwzorowania bez wag.
- etap 5 – import (`importer.py` bez QGIS, `import_dialog.py`): warstwa wektorowa projektu (dowolna geometria,
  tylko zaznaczone) lub CSV (separator, kodowanie, przecinek dziesiętny, kolumny X/Y, układ), automatyczne
  dopasowanie pól (PRNG, OSM), typy PL/EN/DE, duplikaty = odległość ≤ tolerancja i ta sama nazwa (także dawna,
  także w obrębie importu), dopisywanie nazw dawnych, zestaw istniejący lub nowy
  (`tests/test_importer.py`, `tests/library_import_qgis.py`). Pobieranie z sieci (Overpass, usługi PRNG) – później.
- etap 6 – macierz pokrycia (`coverage.py` bez QGIS, `coverage_dialog.py`): punkty zestawu × analizy z punktami
  zestawu (wg roku), kody sure/uncertain/outlier/illegible/disabled/absent, odstające z testu Baardy wybranym
  modelem liczone na żądanie, punkty wspólne wszystkim mapom, odstające na ≥ 2 mapach, eksport CSV/JSON,
  dwuklik – wczytanie analizy (`tests/test_coverage.py`, `tests/library_coverage_qgis.py`).
  Pilot „Kielecczyzna-18” (M2 Zannoni, M5 TKKP Iłża) – na danych użytkownika.
- etap 6b – jednoznaczne powiązanie z biblioteką (po pierwszych testach w QGIS): ikona powiązania przy numerze
  wiersza tabeli GCP, wiersze „brak na mapie” w tabeli (uwagi, usuwanie, dwuklik – wskazanie), propozycja punktu
  biblioteki przy kliknięciu do 40 px, „Powiąż” (`library.match_point`: ta sama nazwa do 5 km, dowolny do 500 m),
  „Następny z zestawu” odrzuca zaczęty krok 1/2, niezapisane zmiany (sygnatura punktów, *Zapisz analizę \**,
  pytanie przy wczytaniu innej analizy), macierz pokrycia ze stanem bieżącym otwartej analizy
  (`build_coverage(..., current=…)`, kolumna `CURRENT`) (`tests/library_link_qgis.py`).
- etap 7 – mapy i arkusze: schemat 2 (`maps`, `analyses.map_id`; migracja 1 → 2 łączy analizy o tym samym
  tytule), `MapInfo`, `save_map` (opis mapy kopiowany do arkuszy), `delete_map`, `move_analysis`; okno opisu
  arkusza z listą map (`maps_dialog.AnalysisDialog`), okno „Mapy i arkusze” (`MapsDialog`), macierz pokrycia
  według arkusza lub mapy (`build_coverage(..., by=…)`, łączenie arkuszy wg `PRIORITY`)
  (`tests/test_library.py`, `tests/test_coverage.py`, `tests/library_maps_qgis.py`).

## Etapy

0. Gałąź, wersja `0.3.0-dev` (experimental), ten plik.
1. `library.py` – schemat, migracje, CRUD, zestawy, wyszukiwanie (bez znaków diakrytycznych), testy.
2. Warstwa biblioteki na mapie głównej: styl wg typu, etykiety, filtr po zestawie, wyróżnienie użytych.
3. Dodawanie GCP z biblioteki: przyciąganie, wyszukiwarka z odległością, „następny brakujący z zestawu”;
   `GCP.uid`, kolumna statusu w tabeli punktów.
4. Jakość dla mapy: `analyses` + `gcp_links`, status i komentarz, zapis/odczyt analizy w bibliotece
   (eksport `.points` zostaje), ostrzeżenie o zmienionych współrzędnych, opcja wag a priori.
5. Import (CSV, dowolna warstwa wektorowa z mapowaniem pól), duplikaty, zestaw z zaznaczenia.
6. Macierz pokrycia punkt × mapa; pilot „Kielecczyzna-18” (M2 Zannoni ark. 14, M5 TKKP Iłża).
6b. Jednoznaczne powiązanie punktów analizy z biblioteką (tabela GCP = treść analizy, niezapisane zmiany).
7. Mapy i arkusze: tabela `maps` (schemat 2, migracja wg tytułu), okno map i arkuszy (nowy arkusz, edycja,
   usuwanie), wybór mapy z listy przy zapisie, macierz według mapy lub arkusza.
8. Obsługa zestawów: zestaw roboczy oddzielony od filtra mapy, okno punktów zestawu, wybór zestawu w zakładce GCP,
   zestaw z punktów analizy lub zasięgu mapy, mniej przycisków.
9. Pilot „Kielecczyzna-18” na danych użytkownika.
10. Wydanie 0.3.0: instrukcja, PR do `main`, wersja bez `-dev`.
Później: tryb serii (statystyki, trajektorie, kopiowanie błędów, raport), import z sieci (Overpass, PRNG),
wagi w estymacji odwzorowania.

Na bieżąco: tłumaczenia EN, testy, dokumentacja.
