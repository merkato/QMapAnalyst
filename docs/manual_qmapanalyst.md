# Instrukcja obsługi wtyczki QMapAnalyst

**QMapAnalyst** to wtyczka QGIS do kartometrycznej analizy dawnych map. Inspirowana programem MapAnalyst pozwala badać dokładność i lokalne zniekształcenia mapy, szacować jej skalę papierową oraz **odwzorowanie kartograficzne, w jakim została wykonana** – w całości w środowisku QGIS.

> Ilustracje przedstawiają arkusz VI szkicu indykacyjnego katastru austriackiego gminy Milówka (skala 1:2880, Archiwum Narodowe w Krakowie). Punkty GCP na ilustracjach są demonstracyjne – służą pokazaniu interfejsu, a nie są georeferencją arkusza.

---

## 1. Mapa dawna

Panel wtyczki (domyślnie po prawej stronie) zawiera u góry podgląd skanu mapy dawnej z paskiem narzędzi:

| Przycisk | Działanie |
|---|---|
| Wczytaj skan | wczytuje niezgeoreferowany skan (`.tif`, `.jpg`, `.png`, `.jp2`…) |
| Przesuwanie / powiększanie / pomniejszanie / pełny widok | nawigacja po skanie (pod podglądem widać współrzędne kursora w pikselach) |
| Dodaj GCP | tryb dodawania par punktów (patrz niżej) |
| Synchronizuj | po obliczeniu transformacji mapa główna QGIS podąża za widokiem skanu |
| Nakładki (n) | rozwija listę nakładek wygenerowanych na mapie dawnej – zaznaczenie decyduje o ich widoczności (także na stronach PDF z mapami) |
| Kosz (u góry) | usuwa wszystkie nakładki z podglądu mapy dawnej |

Dolny panel z zakładkami można zmniejszyć, przeciągając uchwyt między nim a podglądem – zawartość zakładek jest wtedy przewijana. Nakładki te żyją w przestrzeni pikseli skanu, dlatego **nie są dodawane do drzewa warstw** mapy głównej.

---

## 2. Zakładka GCP

![Zakładka GCP: punkty na skanie i tabela z błędami, LOO i resztą standaryzowaną w](gcp_table.png)

1. Włącz **Dodaj pary** (lub przycisk na pasku mapy dawnej).
2. Kliknij punkt na skanie (żółty znacznik), a potem odpowiadający mu punkt na mapie głównej QGIS.
3. Tryb pozostaje aktywny – możesz od razu dodawać kolejne pary. Wyłącz go ponownym kliknięciem. Zmiana narzędzia mapy głównej w trakcie kroku 2 anuluje bieżący punkt.

**Wspomaganie dodawania punktów:** przy włączonym *Przeliczaj automatycznie* transformacja liczy się sama, gdy tylko jest wystarczająco wiele punktów (Helmert – 2, afiniczna – 3), bez ręcznego klikania *Oblicz*. Od tej chwili:

* w kroku 2 na mapie głównej pojawia się **żółte kółko** w przewidywanym położeniu punktu,
* po włączeniu **Synchronizuj** mapa główna centruje się na tym położeniu, a widoki obu map podążają za sobą **w obie strony** (przesuwanie/powiększanie jednej mapy przesuwa drugą).

Im więcej punktów, tym trafniejsze przewidywanie (powyżej 3 punktów uwzględniane są też lokalne zniekształcenia mapy).

Tabela punktów:

* **Akt.** – włącza/wyłącza punkt w obliczeniach (przycisk *Wł./wył.* przełącza wszystkie zaznaczone),
* **X/Y dawna, X/Y ref.** – współrzędne; dwuklik w komórkę pozwala je poprawić,
* **Błąd** – odległość punktu transformowanego od referencyjnego (czerwony = największy lub odstający),
* **LOO** – błąd walidacji krzyżowej: jak daleko „chybia” punkt, gdy transformację wyznaczono bez niego,
* **w** – reszta standaryzowana (test Baardy); |w| > 3,29 oznacza punkt podejrzany o błąd identyfikacji.

Dwuklik w numer wiersza centruje oba widoki na punkcie; zaznaczone punkty są wyróżnione na obu mapach.

**Układ odniesienia** punktów jest zapamiętywany przy pierwszym punkcie (lub odczytywany z nagłówka `#CRS` pliku `.points`). Późniejsza zmiana układu projektu nie psuje już współrzędnych – znaczniki są przeliczane automatycznie.

Zapis/odczyt: format Georeferencera QGIS (`.points`) oraz CSV (separator `,` lub `;`). Przy wczytywaniu możesz zastąpić istniejące punkty albo je uzupełnić; niepoprawne wiersze są pomijane z informacją.

---

## 3. Zakładka Analiza

![Zakładka Analiza: raport dokładności transformacji afinicznej](analysis.png)

Wybierz **transformację**:

* **Helmert (4 parametry)** – skala, obrót, przesunięcie; zachowuje kształty (min. 2 punkty),
* **Afiniczna (6 parametrów)** – dodatkowo różne skale osi i skos, np. skurcz papieru (min. 3),
* **Wielomianowa 2. stopnia (12 parametrów)** – łagodne nieliniowe zniekształcenia (min. 6).

Ustaw **rozdzielczość skanu (DPI)** i kliknij **Oblicz transformację**. Przy włączonym *Przeliczaj automatycznie* każda zmiana punktów przelicza analizę i odświeża istniejące warstwy wynikowe.

Raport zawiera:

* **Dokładność dopasowania** – liczba obserwacji nadliczbowych, błąd średni jednostkowy σ₀, średni błąd położenia punktu m<sub>p</sub> = σ₀·√2, RMSE całkowity i w osiach, średnia/mediana/odchylenie błędów, CE90/CE95, RMSE walidacji krzyżowej oraz **błąd w milimetrach na papierze** (z oceną dokładności graficznej),
* **Parametry transformacji z błędami średnimi** – skala, obrót, skos i stosunek skal osi (wskaźnik skurczu papieru),
* **Skalę mianowaną** dla 150/300/600 DPI i wybranej rozdzielczości, z niepewnością,
* **Punkty odstające** według testu Baardy,
* **Porównanie modeli** Helmert / afiniczna / wielomianowa (σ₀, RMSE, LOO, ΔAICc),
* **Zniekształcenia lokalne** z odwzorowania TPS: zakres skali lokalnej, jej zmienność, zakres rotacji, zniekształcenie kątowe 2ω i anizotropia (elipsa Tissota),
* **Autokorelację przestrzenną reszt** (Moran I, test permutacyjny) – istotna autokorelacja wskazuje na błędy systematyczne (np. łączenie arkuszy, różne źródła pomiarów).

---

## 4. Zakładka Odwzorowanie

![Zakładka Odwzorowanie: ranking kandydatów i siatka geograficzna najlepszego z nich na skanie](projection.png)

Wtyczka dopasowuje do punktów GCP 15 odwzorowań istotnych dla kartografii XV–XX w. Współrzędne punktów referencyjnych są przeliczane na długość i szerokość geograficzną, rzutowane w każdym odwzorowaniu (na sferze), a następnie dopasowywane do pikseli skanu. Parametry kształtu (południk osiowy λ₀, środek φ₀, równoleżniki standardowe φ₁, φ₂) są optymalizowane. Kandydaci są szeregowani kryterium **AICc**, które karze za liczbę dopasowanych parametrów.

| Rodzina | Odwzorowania |
|---|---|
| walcowe | Plate carrée, walcowe równoodległościowe z równoleżnikiem standardowym, Mercatora |
| walcowe poprzeczne | Cassini–Soldnera, Gaussa–Krügera |
| pseudowalcowe / pseudostożkowe | Sansona–Flamsteeda, **Bonne'a**, trapezowe (Donisa) |
| stożkowe | równoodległościowe (De l'Isle'a), Lamberta wiernokątne, Albersa |
| azymutalne | Lamberta równopolowe, stereograficzne, Postela |
| polikoniczne | wielostożkowe Hasslera (bliskie poliedrycznemu w skali arkusza) |

Dla porównania w rankingu pojawia się też **układ odniesienia projektu** (np. EPSG:2180).

* **Dopasowanie:** *Podobieństwo (Helmert)* – zalecane; *Afiniczne* – gdy papier skurczył się nierównomiernie (zmniejsza jednak zdolność rozróżniania odwzorowań).
* Tabela: σ₀ [px], ΔAICc, **waga Akaike** (względne prawdopodobieństwo modelu) i skala główna mapy.
* Panel szczegółów: opis historyczny, parametry z błędami, λ₀ względem południków **Ferro, Paryża, Pułkowa i Warszawy**, ostrzeżenia.
* **Siatka geograficzna na mapie dawnej** rysuje południki i równoleżniki wybranego odwzorowania na skanie – najlepszy test wizualny: porównaj ją z siatką narysowaną na mapie.

### Test hipotezy (znane parametry)

Przy pojedynczym arkuszu ranking zwykle nie rozstrzyga – pewniejszy jest **test hipotezy** (sekcja zwijana pod rankingiem):

1. Wybierz zestaw parametrów znanej mapy (np. *Topograficzna Karta Królestwa Polskiego* – Bonne, λ₀ = 38°42′30″ od Ferro, φ₁ = 52° N) albo *własne parametry*.
2. Wpisz kąty w dowolnej postaci: `21°02′30″ E`, `21 2 30`, `21.0417`, `17°40' W`. Długość λ₀ można podać względem Greenwich, Ferro, Paryża, Pułkowa lub Warszawy.
3. Kliknij **Testuj hipotezę i rysuj siatkę**. Odwzorowanie o ustalonych parametrach trafia do rankingu (kursywą, kolor purpurowy) – dopasowywane jest tylko położenie, skala i obrót siatki – a na skanie pojawia się jego siatka (linia przerywana).

Panel szczegółów porównuje hipotezę ze swobodnym dopasowaniem tego samego odwzorowania. **ΔAICc < 2** – hipoteza zgodna z danymi; **≥ 10** – wyraźnie odrzucona. Najmocniejszym testem jest jednak wizualne porównanie siatki hipotezy z podziałką ramki mapy. Siatki kolejnych hipotez i kandydatów są osobnymi nakładkami, więc można je porównywać na skanie.

**Wskazówki interpretacyjne:**

* Rozróżnianie odwzorowań wymaga dużego zasięgu (najlepiej > 2–3°) i wielu punktów (> 15). Dla map lokalnych wszystkie odwzorowania dają praktycznie tę samą geometrię – wtyczka to sygnalizuje.
* Punkty z błędami grubymi silnie przesuwają parametry odwzorowania. Wtyczka wskazuje podejrzane punkty – sprawdź je w zakładce *Analiza*, wyłącz i powtórz estymację.
* Parametr z błędem > 10° jest praktycznie niewyznaczalny w danym zasięgu.
* Różnica ΔAICc < 2 oznacza modele równorzędne; o wyborze powinna wtedy zdecydować wiedza historyczna (epoka, wykonawca, seria map).

---

## 5. Zakładka Warstwy

![Zakładka Warstwy: siatka PUWG 1992 (oczko 200 m), izolinie skali i wektory przesunięć na skanie](results.png)

Przyciski z ikonami (wyglądającymi jak legenda tworzonej warstwy) układają się w wierszach i zawijają przy wąskim panelu; pełny opis pokazuje podpowiedź po najechaniu kursorem. Ikona kosza na dole zakładki usuwa warstwy wynikowe mapy głównej.

**Parametry:** promień pewności (strefa wokół punktów uznawana za wiarygodną), oczko siatek (w jednostkach układu odniesienia), powiększenie wektorów i okręgów, liczba izolinii.

**Mapa główna** (warstwy w grupie *QMapAnalyst* drzewa warstw):

* **Punkty z resztami** – wszystkie statystyki punktów (reszty, LOO, w) w atrybutach; punkty odstające są wyróżnione,
* **Wektory przesunięć** (strzałki) i **okręgi błędów**,
* **Siatka zniekształceń** – linia ciągła w obszarze pewnym, przerywana w strefie ekstrapolacji.

**Mapa dawna** (nakładki na skanie):

* **Wektory przesunięć** i **okręgi błędów** – wektory są poprawnie obracane do układu skanu,
* **Siatka układu odniesienia** – współczesna siatka (np. kilometrowa PUWG 1992) wygięta na skanie, z etykietami,
* **Izolinie skali, rotacji i zniekształceń kątowych 2ω**.

Ponowne wygenerowanie warstwy zastępuje poprzednią wersję (z zachowaniem jej widoczności). Warstwy usunięte ręcznie z projektu są bezpiecznie pomijane.

---

## 6. Zakładka Eksport

Wybierz folder i zaznacz, co zapisać. Nazwy plików zaczynają się od nazwy skanu (np. `Charlottenbrunn_raport.pdf`):

* **Raport analizy – PDF** – wielostronicowy raport A4 z nagłówkiem (tytuł, model, układ, DPI) i numeracją stron: dane wejściowe, dokładność, parametry z błędami, skala, punkty odstające, porównanie modeli, zniekształcenia lokalne, autokorelacja, **tabela wszystkich punktów z resztami**, ranking odwzorowań oraz szczegóły najlepszego kandydata i testowanych hipotez,
* **Raport analizy – TXT** – ta sama treść jako tekst z wyrównanymi kolumnami (UTF-8) – do archiwizacji, wklejenia do publikacji lub porównań,
* **Mapy z nakładkami – PDF** – strona A4 (poziomo) ze wszystkimi widocznymi nakładkami razem, a następnie po jednej stronie na każdą nakładkę, na tle skanu,
* **TIFF** – bieżący widok mapy dawnej z nakładkami (podwójna rozdzielczość ekranu, z plikiem `.tfw`),
* **CSV punktów** – współrzędne, punkty transformowane, reszty, LOO i w,
* **CSV odwzorowań** – pełny ranking z parametrami.

Raport można też zapisać bezpośrednio z zakładki **Analiza** przyciskiem **Zapisz raport…** – w formacie PDF, TXT lub HTML (format wynika z rozszerzenia pliku). Raport jest w języku interfejsu QGIS.

---

## 7. Uwagi techniczne

* **Język:** interfejs, raport analizy (w panelu i w PDF), komunikaty, nazwy warstw i legendy są po polsku albo po angielsku – zgodnie z językiem interfejsu QGIS (każdy język inny niż polski → angielski). W wersji polskiej liczby w raporcie i tabelach mają przecinek dziesiętny; pliki CSV zawsze używają kropki.

* Obliczenia wykorzystują NumPy i SciPy – oba są w oficjalnych instalatorach QGIS (Windows, macOS); matplotlib nie jest już wymagany. Gdy pakietu brakuje (minimalna instalacja OSGeo4W, niektóre dystrybucje Linuksa), wtyczka pokazuje polecenie instalacji dla danej platformy, np. OSGeo4W Setup → *Advanced Install* → *Libs* → `python3-scipy`, `sudo apt install python3-scipy` (Debian/Ubuntu), `sudo dnf install python3-scipy` (Fedora). Bez NumPy panel się nie otworzy; bez SciPy działa, ale bez estymacji odwzorowania, lokalnych zniekształceń TPS i izolinii.
* Błędy w trakcie działania są zapisywane w panelu *Komunikaty dziennika* (zakładka *QMapAnalyst*), a pasek stanu wtyczki pokazuje ich krótki opis.
* Silnik obliczeniowy można testować poza QGIS: `python-qgis.bat tests/test_engine.py`.
