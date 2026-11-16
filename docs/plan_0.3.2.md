# QMapAnalyst 0.3.2 – rektyfikacja i wyrównanie wydawnictw

Szkic z 16.11.2026, na podstawie wpasowania 9 arkuszy mapy Schmettaua (Górny Śląsk, 1:50 000,
93 punkty biblioteki). Nic z tego nie jest jeszcze zaimplementowane we wtyczce.

Wersja 0.3.2, a nie 0.4.0: wydawnictwa wieloarkuszowe i serie pojawiły się w 0.3.0, a rektyfikacja
rozwija ten sam moduł. Przy tym wydaniu zamykamy też zgłoszenie #13 (usunięcie `supportsQt6` z `metadata.txt`).

## Powiązane zgłoszenia

| Zgłoszenie | Co wchodzi do 0.3.2 |
|---|---|
| [#13](https://github.com/merkato/QMapAnalyst/issues/13) supportsQt6 | usunięcie flagi z `metadata.txt` (`qgisMaximumVersion=4.99` już jest) |
| [#8](https://github.com/merkato/QMapAnalyst/issues/8) łączne wyrównanie arkuszy z punktami wiążącymi | część analityczna wyrównania wydawnictwa (sekcja „Wyrównanie blokowe – analiza”) |
| [#5](https://github.com/merkato/QMapAnalyst/issues/5) wykrywanie map kopiowanych | korelacja reszt z testem permutacyjnym jest od 0.3.0; dochodzi porównanie pól korekcji na siatce i test Mantela |

Poza zakresem: [#6](https://github.com/merkato/QMapAnalyst/issues/6) (siatka kartograficzna λ/φ – osobny moduł
estymacji odwzorowania; przy wydawnictwie mogłaby później zasilić wspólną estymację) i
[#7](https://github.com/merkato/QMapAnalyst/issues/7) (skala w jednostkach historycznych).

## Ustalenia (16.11.2026)

1. **Biblioteka (GeoPackage) przechowuje zasięgi ramek arkuszy.** Dziś `analyses.frame` trzyma ramkę
   na skanie. Dochodzi warstwa poligonowa zasięgów arkuszy w układzie biblioteki (wynik wyrównania)
   i położenie arkusza w siatce wydawnictwa.
2. **Arkusze zachodzące na siebie: suwak „przycinanie ↔ blendowanie”.** Na lewym końcu strefa zakładki
   należy do arkusza o wyższym priorytecie (przycięcie do pierwszego). Przesuwanie w prawo zwiększa
   szerokość płynnego przejścia (blendowanie, w metrach).
3. **Wygładzanie pola korekcji: suwak.** Domyślnie pozycja „auto” (walidacja krzyżowa), a użytkownik
   może ustawić sztywniej (bliżej modelu globalnego) albo luźniej (bliżej TPS interpolującego).
4. **Układ arkuszy w siatce wykrywany po wpasowaniu.** Wstępne wpasowanie afiniczne każdego arkusza,
   potem sąsiedztwo ramek (wspólne krawędzie, odległość narożników) wyznacza wiersz i kolumnę.
   Użytkownik może je poprawić, a wynik zapisuje się w bibliotece.
5. **Blendowanie bez nowych zależności (tylko GDAL i numpy).**
   - *Podgląd:* każdy arkusz jako osobna warstwa w grupie projektu. Jego VRT ma kanał alfa z maski
     przejścia w niskiej rozdzielczości (np. 1/8), skalowanej w VRT z `resampling="bilinear"`.
     Maskę liczy numpy analitycznie, jako odległość piksela od krawędzi zasięgu arkusza
     (zasięgi to poligony z biblioteki), więc scipy nie jest potrzebny. Mieszanie robi QGIS przy
     rysowaniu warstw z kanałem alfa. Zmiana suwaka to tylko przeliczenie małych masek.
   - *Eksport:* ta sama maska w pełnej rozdzielczości, mieszanie blokami w numpy, zapis jednej mozaiki COG.
   - Bez funkcji pikseli w Pythonie w VRT: GDAL domyślnie je blokuje ze względów bezpieczeństwa.

## Problem

Wtyczka liczy dziś **modele globalne** (Helmert, afiniczny, wielomian 2. stopnia). Taki model opisuje mapę
jako całość, a reszty traktuje jako informację o błędach mapy. Do **analizy** dokładności to dobra metoda.
Do **rektyfikacji**, czyli zrobienia rastra, który pokrywa się z mapą współczesną, jest za sztywna.
Mapy dawne mają lokalne zniekształcenia:
- kompilacja z różnych zdjęć,
- tereny kartowane szkicowo albo przejęte z innych źródeł,
- błędy łączenia arkuszy.

Przykład z mapy Schmettaua:

- **Arkusz 218 (Głubczyce, Karniów, Kietrz).** W południowym pasie zachodnie punkty (Karniów, Lowitz) leżą
  na mapie o 1,2–1,4 km za daleko na wschód, a wschodnie (Rosen, Knispel, Kietrz) o 0,6–1,7 km za daleko
  na zachód. To ściśnięcie wschód–zachód o kilkanaście procent na odcinku około 20 km.
  Żadna transformacja całego arkusza tego nie usunie.
- **Arkusz 220 (Bytom).** Młyny pasują, a miasto jest przesunięte. To zniekształcenie lokalne, a nie globalne.
- **Arkusze wpasowywane osobno się rozjeżdżają.** Przy osobnym wpasowaniu afinicznym narożniki sąsiadów
  rozchodzą się średnio o 2,1 km (maksymalnie 4,9 km), co w mozaice daje szczeliny i zakładki.

Georeferencer QGIS ma TPS, ale tylko **interpolujący**. Każdy punkt, także błędnie zidentyfikowany, trafia
dokładnie w swoje miejsce. Między punktami mogą powstawać fałdy, a poza obszarem punktów obraz „odlatuje”.
Każdy arkusz liczy się osobno, więc styki się rozjeżdżają.

## Zasada: analiza ≠ rektyfikacja

1. **Analiza** (jak dotąd): model globalny. Reszty to wynik, czyli dokładność mapy, wektory błędów,
   seria czasowa i ślad kopiowania. Tego nie zmieniamy.
2. **Rektyfikacja** (nowe): model globalny plus **pole korekcji lokalnej**, które usuwa reszty w sposób
   kontrolowany. Wynikiem jest raster do nakładania na mapę współczesną oraz raport, ile i gdzie poprawiono.

Te dwie rzeczy rozdzielamy w interfejsie i w raportach. Mapa po rektyfikacji „wygląda dokładnie”,
ale o jej dokładności mówi analiza, a nie raster.

## Rodzina transformacji do rektyfikacji

| Metoda | Charakter | Kiedy | Ryzyko |
|---|---|---|---|
| Globalna (Helmert / afin. / wielomian 2) | sztywna, gładka | mapy pomiarowe, mało punktów | zostawia lokalne błędy |
| **Wygładzony TPS** (regularyzowany) | gładka, nie przechodzi dokładnie przez punkty | **domyślna** dla map dawnych | dobór wygładzania (rozwiązanie: walidacja krzyżowa) |
| TPS interpolujący (jak Georeferencer) | dokładnie w punktach | gęsta, pewna sieć punktów | fałdy, błędne punkty ciągną obraz |
| Trójkąty (Delaunay, afin. w trójkącie) | dokładnie w punktach, ciągła, bez fałd | „gumowanie” jak ArcGIS Adjust | załamania na krawędziach trójkątów |
| MLS (moving least squares, afin. ważona odległością) | lokalnie sztywna, gładka | mapy z lokalnymi przesunięciami całych fragmentów | dobór promienia |

- **Wygładzony TPS:** pole korekcji z reszt modelu globalnego. Parametr wygładzania dobiera się
  automatycznie walidacją krzyżową (leave-one-out), a użytkownik może go zmienić suwakiem „sztywność”.
  Punkty z obniżoną wagą (test Baardy, Huber) wpływają słabiej.
  - Schmettau, 9 arkuszy: RMSE 763 m → 188 m po korekcji. Błąd LOO 640 m, czyli pole poprawia też
    miejsca między punktami, a nie tylko same punkty.
  - Na arkuszu 218: Karniów 1472 → 61 m, Kietrz 1710 → 372 m.
- **Bez scipy:** wygładzony TPS dla kilkuset punktów to jeden układ równań (n+3)×(n+3), który rozwiązuje
  `numpy.linalg.solve`. Walidację krzyżową da się policzyć z jednego rozkładu macierzy, bez n osobnych
  rozwiązań. Prototyp na mapie Schmettaua używał `scipy.interpolate.RBFInterpolator`, ale we wtyczce
  zastąpi go numpy.
- **Kontrola fałd** dla wszystkich metod: znak jakobianu na siatce kontrolnej, ostrzeżenie i obszar
  z fałdą na mapie.

## Wyrównanie wydawnictwa wieloarkuszowego (styki bez szczelin)

Model sprawdzony na mapie Schmettaua:

1. **Ramki arkuszy** (4 rogi, już zapisywane w `analyses.frame`) i **układ arkuszy w siatce**
   (wiersz, kolumna) – w bibliotece, zob. „Schemat biblioteki”.
2. **Niewiadome to wspólne węzły siatki**, czyli narożniki ramek w układzie odniesienia. Arkusz jest
   rozpięty biliniowo między swoimi 4 węzłami. Obserwacje to punkty wszystkich arkuszy naraz. Układ jest
   liniowy, wyrównywany odpornie (Huber), ze słabą regularyzacją kształtu siatki.
3. **Pole korekcji lokalnej** (wygładzony TPS) liczone **we współrzędnych terenowych**, a nie arkuszy.
   Krawędź wspólna dwóch arkuszy to te same punkty terenu, więc po korekcji też się pokrywa.
4. **Przetworzenie:** dla każdego arkusza gęsta siatka GCP (np. 25×25), potem `gdalwarp -tps`.
   Linia cięcia to zagęszczony obwód węzłów po korekcji. Wspólna siatka pikseli (`-tap`) daje mozaikę
   bez zakładek. Format: COG z kompresją WEBP i kanałem alfa, mozaika VRT. Zasięgi arkuszy po wyrównaniu
   zapisywane w bibliotece (skorowidz z RMSE).
5. **Arkusze zachodzące na siebie** (zakładki na papierze): zamiast wspólnych węzłów – punkty wiążące
   w strefie zakładki (ten sam obiekt na obu arkuszach) jako dodatkowe obserwacje. W mozaice suwak
   „przycinanie ↔ blendowanie” (ustalenie 2): przycięcie do arkusza o wyższym priorytecie albo płynne
   przejście o zadanej szerokości (maska alfa z odległości od krawędzi).

Każdą metodę z tabeli wyrażamy jako **gęstą siatkę GCP** (skan → teren), więc wystarczy GDAL i nie trzeba
własnego przetwarzania rastra. Siatka jest na tyle gęsta, że TPS GDAL między węzłami odtwarza metodę.

## Wyrównanie blokowe – analiza (#8)

Część analityczna wydawnictwa, zgodnie z zasadą „analiza ≠ rektyfikacja”:

- **Model:** transformacja na arkusz (Helmert albo afiniczna, ten sam model dla wszystkich arkuszy – jak
  w serii czasowej; różne modele na arkusz na razie nie).
- **Obserwacje:**
  - GCP każdego arkusza;
  - **punkty wiążące**: ten sam obiekt na dwóch arkuszach, z punktem biblioteki albo bez niego
    (para skan A ↔ skan B);
  - **wspólne narożniki ramek** sąsiednich arkuszy jako warunki zszycia (z wagą).
- **Wyniki:**
  - σ₀ całości i każdego arkusza, macierz kowariancji;
  - test Baardy dla GCP i punktów wiążących;
  - **wektory niezgodności na stykach**, czyli to, co przy osobnych wpasowaniach dawało u Schmettaua
    szczeliny do 4,9 km;
  - wskazanie arkusza „odstającego”.
- **Warstwy i raport:** zasięgi arkuszy (`sheet_footprints`), wektory niezgodności na stykach, raport
  wydawnictwa rozszerzony o wyrównanie blokowe (obok dzisiejszej zakładki „Zakładki”).
- **Test syntetyczny (z #8):** 4 arkusze z jednej transformacji plus szum, część arkuszy tylko z punktami
  wiążącymi. Transformacje mają zostać odtworzone, a arkusz z błędem przesunięcia wykryty.
- Wspólna estymacja odwzorowania dla całej mapy – po wyrównaniu blokowym, na wszystkich GCP w jednym
  układzie (później, razem z #6).

## Narzędzie „Rektyfikacja”

Okno, które robi coś, czego Georeferencer nie potrafi:

- **Wejście:** skan i plik `.points` albo analiza z biblioteki. Dla wydawnictwa: mapa z biblioteki
  (wszystkie arkusze).
- **Metoda:** z tabeli powyżej. Suwak wygładzania z pozycją „auto” (walidacja krzyżowa); podgląd RMSE
  i błędu walidacji krzyżowej przy każdej pozycji suwaka.
- **Zakładki arkuszy:** suwak „przycinanie ↔ blendowanie” i kolejność priorytetu arkuszy.
- **Ramka:** przycięcie do ramki mapy z analizy (bez marginesów, listew skanera i pieczątek poza ramką).
- **Wyjście:** rozdzielczość (domyślnie skala × dpi), format (COG WEBP lub JPEG, GeoTIFF), kanał alfa,
  mozaika VRT dla wydawnictwa, dodanie do projektu.
- **Raport:**
  - RMSE modelu globalnego i po korekcji, błąd LOO, punkty o obniżonej wadze;
  - mapa wielkości korekcji (raster lub izolinie), wektory korekcji, ostrzeżenia o fałdach;
  - dla wydawnictwa: szczeliny na stykach przy wpasowaniu osobnym wobec wspólnego.
- **Zapis ustawień** w pliku obok wyniku (`*.rectify.json`, odpowiednik `fit.json`), żeby wynik można było
  odtworzyć. Bez historii w bibliotece (ustalenie 1 z planu 0.3).

## Schemat biblioteki (wersja 4)

| Zmiana | Pola | Uwagi |
|---|---|---|
| `analyses` | `grid_row`, `grid_col`, `priority` | położenie arkusza w siatce wydawnictwa, kolejność przy zakładkach |
| nowa warstwa `sheet_footprints` (poligon, układ biblioteki) | `analysis_id`, `map_id`, `method`, `smoothing`, `rmse_m`, `rmse_corr_m`, `loo_m`, `modified` | zasięg arkusza po wyrównaniu; skorowidz wydawnictwa |

- Migracja `_migrate_3` jak poprzednie: idempotentna, z kopią `.v3.bak.gpkg`.
- Siatka nie musi być regularna: brakujące arkusze po prostu nie mają analizy, a arkusze przesunięte
  względem siatki łączą się punktami wiążącymi zamiast wspólnych węzłów.
- Zasięgi to wynik, a nie dane wejściowe. Są nadpisywane przy każdym wyrównaniu (zgodnie z ustaleniem 1
  planu 0.3: bez historii obliczeń).

## Seria czasowa a wyrównanie

Mapy różnych lat nie są wyrównywane wspólnie, bo każda ma własne błędy. Seria może jednak korzystać
z rektyfikacji i biblioteki:

- **Spójność punktu w serii.** Duża reszta tego samego punktu na wszystkich mapach sugeruje błąd
  współrzędnych w bibliotece albo zmianę obiektu, a nie błąd mapy. Takie punkty trzeba wskazać
  przed rektyfikacją.
- **Pola korekcji porównywalne między mapami (#5).** Podobne pola korekcji to drugi, obok korelacji
  reszt, wskaźnik kopiowania (np. Helwig → Mercator):
  - pola na regularnej siatce we wspólnym obszarze (przecięcie otoczek punktów obu map): lokalna skala,
    obrót, anizotropia i wektor korekcji;
  - korelacja pól z testem Mantela albo permutacyjnym (obrót i przesunięcie pola), żeby odróżnić kopię
    od podobieństwa wynikającego z tego samego terenu;
  - mapa różnic pól, ostrzeżenie przy zbyt małym zasięgu lub liczbie punktów;
  - normalizacja przy różnej skali obu map: pola względne, skala podzielona przez skalę globalną.
- **Rektyfikacja całej serii jednym ustawieniem:** ta sama metoda, wygładzanie z walidacji krzyżowej
  osobno dla każdej mapy, warstwy w grupie projektu w kolejności lat.

## Etapy (propozycja)

1. **Silnik:** pole korekcji (wygładzony TPS z walidacją krzyżową, trójkąty, MLS), kontrola fałd,
   generowanie gęstej siatki GCP i linii cięcia. Testy na danych syntetycznych (znane zniekształcenie).
2. **Rektyfikacja jednej mapy:** okno, wyjście COG/VRT, raport i warstwa wielkości korekcji.
3. **Wyrównanie wydawnictwa – analiza (#8):** wyrównanie blokowe z punktami wiążącymi i warunkami
   na narożnikach, σ₀, test Baardy, wektory niezgodności na stykach, arkusz odstający.
4. **Wyrównanie wydawnictwa – rektyfikacja:** schemat biblioteki 4 (siatka arkuszy, `sheet_footprints`), wspólne węzły
   i punkty wiążące, korekcja w terenie, mozaika VRT z suwakiem przycinanie ↔ blendowanie. Raport szczelin.
5. **Seria:** spójność punktów w serii, porównanie pól korekcji z testem Mantela (#5), rektyfikacja całej serii.
6. **Wydanie:** #13 (`supportsQt6`), dokumentacja i zrzuty: Schmettau (wydawnictwo), Helwig / Mercator (seria).

## Pytania otwarte

Brak – ustalenia 1–5 zamykają pytania ze szkicu.
