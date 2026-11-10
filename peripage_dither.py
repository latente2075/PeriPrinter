#!/usr/bin/env python3
"""
Stampa un'immagine su una Peripage A6+ con dithering a scelta, mandando alla stampante
le righe gia' pronte a 1 bit (formato del driver ufficiale: 640 punti, 80 byte per riga).

Esempi:
  # anteprima senza stampare
  python peripage_dither.py foto.png --dither stucki --gamma 1.2 --preview prova.png

  # confronto di tutti i metodi su un ritaglio (nessuna stampa)
  python peripage_dither.py foto.png --compare confronto.png --gamma 1.2 --sharpen 80

  # stampa sulla porta COM virtuale del Bluetooth, concentration fissa 1
  python peripage_dither.py foto.png --dither stucki -c 1 --gamma 1.2 --com COM9

  # concentration adattiva per fasce di righe (livelli tarati sulla tua stampante)
  python peripage_dither.py foto.png --row-density --smooth 3 --gamma 1.2 --com COM9

  # disegno al tratto (nessun dithering) ruotato di 90 gradi
  python peripage_dither.py disegno.png --dither threshold --threshold 160 --rotate 90 --com COM9

  # foglio di prova: righello e quadrati per misurare area stampabile (48 mm = 576 punti) e 304 dpi
  python peripage_dither.py --ruler --com COM9

  # legge nome, firmware e batteria dalla stampante
  python peripage_dither.py --status --com COM9

  # scrive il flusso su file invece di stamparlo
  python peripage_dither.py foto.png --dump job.bin

Dipendenze: pillow, numpy; per --com anche pyserial.
"""

import argparse
import re
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

DRIVER_ROW_BYTES = 80   # 640 punti per riga inviata (come fa il driver ufficiale)
HEAD_DOTS = DRIVER_ROW_BYTES * 8
PRINT_WIDTH = 576       # punti realmente stampati: 48 mm a 304 dpi (48 / 25.4 * 304 = 575). Oltre, la testina non stampa
DRIVER_BLOCK_ROWS = 24  # righe per comando 1d 76 30
DRIVER_HEAD = bytes.fromhex("10fffe01" "1b40" "00" "1b4a3c")  # reset, init, avanzamento iniziale
DRIVER_TAIL = bytes.fromhex("10fffe45")                       # fine job
DEFAULT_LEVELS = (0.88, 1.0, 1.0)  # annerimento dei punti a concentration 0/1/2 (tarato a occhio)

# Kernel di diffusione dell'errore: (dx, dy, peso), divisore.
# floyd = veloce e classico; stucki/jarvis/sierra spargono l'errore su piu' pixel
# (meno "vermi", piu' dettaglio nelle foto); atkinson propaga solo 6/8 dell'errore
# (piu' contrasto, ombre e luci pulite); sierra-lite e burkes sono varianti leggere.
KERNELS = {
    "floyd": ([(1, 0, 7), (-1, 1, 3), (0, 1, 5), (1, 1, 1)], 16),
    "jarvis": ([(1, 0, 7), (2, 0, 5),
                (-2, 1, 3), (-1, 1, 5), (0, 1, 7), (1, 1, 5), (2, 1, 3),
                (-2, 2, 1), (-1, 2, 3), (0, 2, 5), (1, 2, 3), (2, 2, 1)], 48),
    "stucki": ([(1, 0, 8), (2, 0, 4),
                (-2, 1, 2), (-1, 1, 4), (0, 1, 8), (1, 1, 4), (2, 1, 2),
                (-2, 2, 1), (-1, 2, 2), (0, 2, 4), (1, 2, 2), (2, 2, 1)], 42),
    "sierra": ([(1, 0, 5), (2, 0, 3),
                (-2, 1, 2), (-1, 1, 4), (0, 1, 5), (1, 1, 4), (2, 1, 2),
                (-1, 2, 2), (0, 2, 3), (1, 2, 2)], 32),
    "sierra-lite": ([(1, 0, 2), (-1, 1, 1), (0, 1, 1)], 4),
    "burkes": ([(1, 0, 8), (2, 0, 4),
                (-2, 1, 2), (-1, 1, 4), (0, 1, 8), (1, 1, 4), (2, 1, 2)], 32),
    "atkinson": ([(1, 0, 1), (2, 0, 1), (-1, 1, 1), (0, 1, 1), (1, 1, 1), (0, 2, 1)], 8),
}

ORDERED = ["bayer", "bluenoise"]

METHODS = list(KERNELS) + ORDERED + ["threshold"]

def error_diffusion(gray: np.ndarray, kernel, divisor, serpentine: bool = True) -> np.ndarray:
    """gray: array 0..255 (0 = nero). Ritorna array bool, True = punto da bruciare.

    Con serpentine=True le righe dispari vengono percorse da destra a sinistra
    (con il kernel specchiato): elimina la direzionalità tipica degli artefatti.
    """
    h, w = gray.shape
    rows = [list(map(float, r)) for r in gray]
    out = np.zeros((h, w), dtype=bool)
    fwd = [(dx, dy, wt / divisor) for dx, dy, wt in kernel]
    rev = [(-dx, dy, wt) for dx, dy, wt in fwd]
    for y in range(h):
        row = rows[y]
        backwards = serpentine and (y & 1)
        xs = range(w - 1, -1, -1) if backwards else range(w)
        taps = rev if backwards else fwd
        for x in xs:
            old = row[x]
            if old >= 128.0:
                new = 255.0
            else:
                new = 0.0
                out[y, x] = True
            err = old - new
            if err == 0.0:
                continue
            for dx, dy, wt in taps:
                xx, yy = x + dx, y + dy
                if 0 <= xx < w and yy < h:
                    rows[yy][xx] += err * wt
    return out

def bayer_matrix(n: int) -> np.ndarray:
    m = np.array([[0, 2], [3, 1]])
    while m.shape[0] < n:
        m = np.block([[4 * m, 4 * m + 2], [4 * m + 3, 4 * m + 1]])
    return m

_BLUE_CACHE = {}

def blue_noise_ranks(n: int = 64, sigma: float = 1.5, seed: int = 7) -> np.ndarray:
    """Matrice di soglie "blue noise" n x n con l'algoritmo void-and-cluster (Ulichney).

    Ritorna i ranghi 0..n*n-1: i punti si distribuiscono in modo uniforme e
    senza strutture regolari, a differenza di Bayer. Calcolata una volta (~1 s).
    """
    key = (n, sigma, seed)
    if key in _BLUE_CACHE:
        return _BLUE_CACHE[key]
    rng = np.random.default_rng(seed)
    idx = np.minimum(np.arange(n), n - np.arange(n))
    g = np.exp(-(idx[:, None] ** 2 + idx[None, :] ** 2) / (2.0 * sigma * sigma))
    total = n * n
    k = total // 10

    def bump(y, x):
        return np.roll(g, (y, x), axis=(0, 1))

    pat = np.zeros((n, n), dtype=bool)
    pat.flat[rng.choice(total, k, replace=False)] = True
    energy = np.zeros((n, n))
    for y, x in zip(*np.nonzero(pat)):
        energy += bump(y, x)

    # 1) rilassa il motivo iniziale: sposta il punto più "affollato" nel vuoto più grande
    for _ in range(10 * total):
        e = np.where(pat, energy, -np.inf)
        y, x = np.unravel_index(np.argmax(e), e.shape)
        pat[y, x] = False
        energy -= bump(y, x)
        e = np.where(pat, np.inf, energy)
        y2, x2 = np.unravel_index(np.argmin(e), e.shape)
        pat[y2, x2] = True
        energy += bump(y2, x2)
        if (y2, x2) == (y, x):
            break

    rank = np.zeros((n, n), dtype=np.int64)
    # 2) ranghi dei punti iniziali: li tolgo dal più affollato al meno
    p, en = pat.copy(), energy.copy()
    for r in range(k - 1, -1, -1):
        e = np.where(p, en, -np.inf)
        y, x = np.unravel_index(np.argmax(e), e.shape)
        rank[y, x] = r
        p[y, x] = False
        en -= bump(y, x)
    # 3) ranghi degli altri: li aggiungo sempre nel vuoto più grande
    p, en = pat.copy(), energy.copy()
    for r in range(k, total):
        e = np.where(p, np.inf, en)
        y, x = np.unravel_index(np.argmin(e), e.shape)
        rank[y, x] = r
        p[y, x] = True
        en += bump(y, x)
    _BLUE_CACHE[key] = rank
    return rank

def ordered_dither(gray: np.ndarray, method: str = "bayer") -> np.ndarray:
    if method == "bluenoise":
        rank = blue_noise_ranks()
        thr = (rank + 0.5) / rank.size * 255.0
    else:
        m = bayer_matrix(8)
        thr = (m + 0.5) / m.size * 255.0
    h, w = gray.shape
    th, tw = thr.shape
    tiled = np.tile(thr, (h // th + 1, w // tw + 1))[:h, :w]
    return gray < tiled

def dither_gray(gray: np.ndarray, method: str, serpentine: bool = True, threshold: float = 128.0) -> np.ndarray:
    """gray: array float 0..255. Ritorna bool, True = punto bruciato.

    Con method="threshold" non c'e' dithering: e' nero chi e' piu' scuro di `threshold` (line art)."""
    if method in KERNELS:
        kernel, div = KERNELS[method]
        return error_diffusion(gray, kernel, div, serpentine)
    if method in ORDERED:
        return ordered_dither(gray, method)
    return gray < threshold  # soglia semplice (line art)

def load_gray(path, width, gamma=1.0, autocontrast=0.0, sharpen=0.0, clarity=0.0, rotate=0) -> np.ndarray:
    """Carica l'immagine e la prepara in toni di grigio float 0..255 alla larghezza della testina.

    Ordine: ruota (gradi in senso orario: 0, 90, 180, 270) -> ridimensiona (LANCZOS) -> autocontrast
    -> sharpen -> clarity -> gamma.
    """
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA") or "transparency" in img.info:
        rgba = img.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, rgba)
    img = img.convert("L")
    if rotate % 360:
        img = img.transpose({90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180,
                             270: Image.Transpose.ROTATE_90}[rotate % 360])
    new_h = max(1, round(img.height * width / img.width))
    img = img.resize((width, new_h), Image.Resampling.LANCZOS)
    if autocontrast:
        img = ImageOps.autocontrast(img, cutoff=autocontrast)
    if sharpen:
        # maschera di contrasto: i punti della termica ammorbidiscono i bordi
        img = img.filter(ImageFilter.UnsharpMask(radius=1.2, percent=int(sharpen), threshold=0))

    gray = np.asarray(img, dtype=np.float32)
    if clarity:
        # contrasto locale: esalta i dettagli rispetto allo sfondo sfocato
        blur = np.asarray(img.filter(ImageFilter.GaussianBlur(max(8.0, width / 16.0))), dtype=np.float32)
        gray = np.clip(blur + (gray - blur) * (1.0 + clarity), 0.0, 255.0)
    if gamma != 1.0:
        # gamma > 1 schiarisce i mezzitoni (utile: la termica tende a scurire)
        gray = 255.0 * np.power(gray / 255.0, 1.0 / gamma)
    return gray

def compare_sheet(gray: np.ndarray, out_path: str, serpentine: bool = True, size: int = 256):
    """Foglio di confronto: ritaglio centrale dell'immagine con ogni metodo, affiancati."""
    h, w = gray.shape
    ch, cw = min(size, h), min(size, w)
    y0, x0 = (h - ch) // 2, (w - cw) // 2
    crop = gray[y0:y0 + ch, x0:x0 + cw]
    tiles = [("originale", np.clip(crop, 0, 255).astype(np.uint8))]
    for m in [k for k in KERNELS] + ORDERED:
        burn = dither_gray(crop, m, serpentine)
        tiles.append((m, np.where(burn, 0, 255).astype(np.uint8)))
    cols = 5
    rows_n = (len(tiles) + cols - 1) // cols
    label_h = 16
    sheet = Image.new("L", (cols * (cw + 4) + 4, rows_n * (ch + label_h + 4) + 4), 200)
    draw = ImageDraw.Draw(sheet)
    for i, (name, arr) in enumerate(tiles):
        cx = 4 + (i % cols) * (cw + 4)
        cy = 4 + (i // cols) * (ch + label_h + 4)
        draw.text((cx + 2, cy + 2), name, fill=0)
        sheet.paste(Image.fromarray(arr, "L"), (cx, cy + label_h))
    sheet.save(out_path)

def ruler_burn() -> np.ndarray:
    """Foglio di prova per misurare area stampabile e risoluzione (640 punti di larghezza).

    Contiene: una linea in alto su tutti i 640 punti con tre barre verticali (a 0, a 573-575 e a 637-639);
    un righello con tacche ogni 32 punti e numeri ogni 64; un quadrato di 304 punti (1 pollice = 25,4 mm)
    e uno di 152 punti. Dove finisce la linea in alto, o l'ultimo numero visibile, c'e' il limite reale
    della stampa; misurando i quadrati si controllano i 304 dpi in orizzontale e in verticale.
    """
    w = HEAD_DOTS
    img = Image.new("L", (w, 490), 255)
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=18)
    except TypeError:  # Pillow piu' vecchio: font bitmap fisso
        font = ImageFont.load_default()
    y = 8
    d.rectangle([0, y, w - 1, y + 2], fill=0)
    for x0 in (0, PRINT_WIDTH - 3, w - 3):
        d.rectangle([x0, y, x0 + 2, y + 40], fill=0)
    y = 66
    d.rectangle([0, y, w - 1, y + 1], fill=0)
    for x in range(0, w + 1, 32):
        long_tick = x % 64 == 0
        xx = min(x, w - 2)
        d.rectangle([xx, y, xx + 1, y + (26 if long_tick else 12)], fill=0)
        if long_tick:
            label = str(x)
            tw = d.textlength(label, font=font)
            d.text((min(max(0, x - tw / 2), w - tw), y + 30), label, fill=0, font=font)
    y = 130
    d.rectangle([40, y, 40 + 303, y + 303], outline=0, width=3)    # 304 x 304 punti = 25,4 mm
    d.rectangle([400, y, 400 + 151, y + 151], outline=0, width=3)  # 152 x 152 punti = 12,7 mm
    d.text((40, y + 312), "304 dots = 25.4 mm", fill=0, font=font)
    d.text((400, y + 160), "152 = 12.7 mm", fill=0, font=font)
    return np.asarray(img) < 128


def raster_cmd(rows_bytes: bytes, height: int) -> bytes:
    """Comando raster del driver: 1d 76 30 00 | 80 byte/riga | altezza | dati."""
    return bytes([0x1D, 0x76, 0x30, 0x00, DRIVER_ROW_BYTES, 0x00, height, 0x00]) + rows_bytes


# --- invio al formato del driver ---

def pad_to_row(burn: np.ndarray) -> np.ndarray:
    """Porta le righe a 640 punti aggiungendo bianco a destra (le righe inviate sono sempre da 80 byte)."""
    w = burn.shape[1]
    if w > HEAD_DOTS:
        raise ValueError(f"larghezza {w} oltre il massimo di {HEAD_DOTS} punti")
    return burn if w == HEAD_DOTS else np.pad(burn, ((0, 0), (0, HEAD_DOTS - w)))


def driver_stream(burn: np.ndarray, concentration=None):
    """Flusso nel formato del driver ufficiale (osservato in una cattura USB).

    Ritorna (corpo, coda): il corpo e' 10fffe01 1b40 00 1b4a78 [+ concentration] + blocchi
    raster da 24 righe separati da un byte 00; la coda (10fffe45) il driver la manda a parte.
    """
    burn = pad_to_row(burn)
    h, w = burn.shape
    pad = (-h) % DRIVER_BLOCK_ROWS  # completa l'ultimo blocco con righe bianche
    if pad:
        burn = np.vstack([burn, np.zeros((pad, w), dtype=bool)])
    packed = np.packbits(burn, axis=1)
    head = DRIVER_HEAD
    blocks = []
    for i in range(0, packed.shape[0], DRIVER_BLOCK_ROWS):
        hdr = bytes([0x1D, 0x76, 0x30, 0x00, DRIVER_ROW_BYTES, 0x00, DRIVER_BLOCK_ROWS, 0x00])
        blocks.append(hdr + packed[i:i + DRIVER_BLOCK_ROWS].tobytes())
    if concentration is not None:  # 10 ff 10 00 nn: come nei test di densita' (0, 1 o 2)
        head += bytes.fromhex("10ff1000") + bytes([concentration])
    return head + b"\x00".join(blocks), DRIVER_TAIL

def adaptive_density(gray, levels=DEFAULT_LEVELS, method="floyd", serpentine=True, band=2):
    """Dithering con concentration scelta per fasce di righe.

    A concentration bassa un punto brucia leggero (annerimento levels[0]), a quella alta quasi nero.
    Per ogni fascia di `band` righe provo le tre concentration, faccio il dithering con la tavolozza
    {bianco, levels[c]} e tengo quella con l'errore di quantizzazione piu' basso: nelle luci
    vince la concentration bassa (punti piu' leggeri, gradazioni piu' fini), nelle ombre quella alta.

    Ritorna (burn, conc, sim): punti da bruciare, concentration per riga, annerimento previsto 0..1.
    """
    kernel, div = KERNELS.get(method, KERNELS["floyd"])
    h, w = gray.shape
    fwd = [(dx, dy, wt / div) for dx, dy, wt in kernel]
    rev = [(-dx, dy, wt) for dx, dy, wt in fwd]
    spill = max(dy for _, dy, _ in kernel)
    rows = [[1.0 - float(v) / 255.0 for v in r] for r in gray]  # annerimento desiderato 0..1
    burn = np.zeros((h, w), dtype=bool)
    conc = np.zeros(h, dtype=np.uint8)
    for y0 in range(0, h, band):
        y1 = min(h, y0 + band)
        best = None
        for c, v in enumerate(levels):
            work = [r[:] for r in rows[y0:min(h, y1 + spill)]]
            b = np.zeros((y1 - y0, w), dtype=bool)
            errsum = 0.0
            half = v / 2.0
            for yy in range(y1 - y0):
                row = work[yy]
                backwards = serpentine and ((y0 + yy) & 1)
                xs = range(w - 1, -1, -1) if backwards else range(w)
                taps = rev if backwards else fwd
                for x in xs:
                    old = row[x]
                    if old >= half:
                        new = v
                        b[yy, x] = True
                    else:
                        new = 0.0
                    err = old - new
                    # errore non limitato + forte penalita' se il punto e' piu' scuro di quanto la concentration
                    # puo' dare (saturazione): evita di scegliere una concentration debole per righe con ombre
                    errsum += abs(err) + (4.0 * (old - v) if old > v else 0.0)
                    err = 0.5 if err > 0.5 else (-0.5 if err < -0.5 else err)  # ma non propago derive
                    if err == 0.0:
                        continue
                    for dx, dy, wt in taps:
                        xx, yw = x + dx, yy + dy
                        if 0 <= xx < w and yw < len(work):
                            work[yw][xx] += err * wt
            if best is None or errsum < best[0]:
                best = (errsum, c, b, work)
        _, c, b, work = best
        burn[y0:y1] = b
        conc[y0:y1] = c
        rows[y0:y0 + len(work)] = work
    lv = np.asarray(levels, dtype=np.float32)
    sim = burn.astype(np.float32) * lv[conc][:, None]
    return burn, conc, sim

def _sliding_max(a: np.ndarray, k: int) -> np.ndarray:
    pad = np.pad(a, k, mode="edge")
    return np.max([pad[i:i + len(a)] for i in range(2 * k + 1)], axis=0)

def _moving_avg(a: np.ndarray, k: int) -> np.ndarray:
    pad = np.pad(a, k, mode="edge")
    return np.convolve(pad, np.ones(2 * k + 1) / (2 * k + 1), mode="valid")

def interleave_concentration(conc: np.ndarray, smooth: int) -> np.ndarray:
    """Trasforma la concentration per fasce (a gradini) in una miscela fine tra livelli vicini.

    Il profilo viene prima esteso verso l'alto (massimo mobile, per non appiattire le ombre),
    poi ammorbidito (media mobile) e infine riconvertito in interi con la sequenza della sezione
    aurea, che e' una sequenza "blue noise" monodimensionale: nelle zone di passaggio le righe di
    due concentration si alternano in modo irregolare invece di formare una fascia netta.
    """
    c = conc.astype(np.float32)
    if smooth > 0:
        c = _moving_avg(_sliding_max(c, smooth), smooth)
    r = (0.5 + np.arange(len(c)) * 0.6180339887498949) % 1.0
    return np.clip(np.floor(c + r), 0, 2).astype(np.uint8)

def dither_rows_fixed(gray, conc, levels, method="floyd", serpentine=True) -> np.ndarray:
    """Diffusione dell'errore con concentration gia' decisa per ogni riga."""
    kernel, div = KERNELS.get(method, KERNELS["floyd"])
    h, w = gray.shape
    fwd = [(dx, dy, wt / div) for dx, dy, wt in kernel]
    rev = [(-dx, dy, wt) for dx, dy, wt in fwd]
    rows = [[1.0 - float(v) / 255.0 for v in r] for r in gray]
    burn = np.zeros((h, w), dtype=bool)
    for y in range(h):
        v = levels[int(conc[y])]
        half = v / 2.0
        row = rows[y]
        backwards = serpentine and (y & 1)
        xs = range(w - 1, -1, -1) if backwards else range(w)
        taps = rev if backwards else fwd
        for x in xs:
            old = row[x]
            if old >= half:
                new = v
                burn[y, x] = True
            else:
                new = 0.0
            err = old - new
            err = 0.5 if err > 0.5 else (-0.5 if err < -0.5 else err)
            if err == 0.0:
                continue
            for dx, dy, wt in taps:
                xx, yy = x + dx, y + dy
                if 0 <= xx < w and yy < h:
                    rows[yy][xx] += err * wt
    return burn

def dither_rows_bluenoise(gray, conc, levels) -> np.ndarray:
    """Dithering ordinato con soglie blue noise: ogni riga e' indipendente dalle altre, quindi
    non ci sono derive tra righe a concentration diversa."""
    h, w = gray.shape
    rank = blue_noise_ranks()
    thr = (rank + 0.5) / rank.size
    th, tw = thr.shape
    tiled = np.tile(thr, (h // th + 1, w // tw + 1))[:h, :w]
    v = np.asarray(levels, dtype=np.float32)[conc][:, None]
    p = np.clip((1.0 - gray / 255.0) / v, 0.0, 1.0)  # copertura necessaria con punti di annerimento v
    return p > tiled

def adaptive_pipeline(gray, levels=DEFAULT_LEVELS, method="floyd", serpentine=True, band=2,
                      smooth=3, row_dither="diffusion"):
    """Concentration adattiva "sfumata": stima per fasce -> miscela fine tra livelli -> dithering.

    Ritorna (burn, conc, sim) come adaptive_density.
    """
    _, conc0, _ = adaptive_density(gray, levels, method, serpentine, band)
    conc = interleave_concentration(conc0, smooth)
    if row_dither == "bluenoise":
        burn = dither_rows_bluenoise(gray, conc, levels)
    else:
        burn = dither_rows_fixed(gray, conc, levels, method, serpentine)
    lv = np.asarray(levels, dtype=np.float32)
    sim = burn.astype(np.float32) * lv[conc][:, None]
    return burn, conc, sim

def adaptive_parts(burn: np.ndarray, conc: np.ndarray, cur=None):
    """Spezza il lavoro in comandi: cambia concentration (10 ff 10 00 nn) solo quando serve,
    poi un comando raster per ogni tratto di righe con la stessa concentration."""
    packed = np.packbits(pad_to_row(burn), axis=1)
    h = len(conc)
    parts, y = [], 0
    while y < h:
        c = int(conc[y])
        y2 = y
        while y2 < h and conc[y2] == c and y2 - y < 255:
            y2 += 1
        part = b""
        if c != cur:
            part += bytes.fromhex("10ff1000") + bytes([c])
            cur = c
        part += raster_cmd(packed[y:y2].tobytes(), y2 - y)
        parts.append(part)
        y = y2
    return parts, cur

def adaptive_stream(burn: np.ndarray, conc: np.ndarray):
    parts, _ = adaptive_parts(burn, conc)
    return DRIVER_HEAD + b"\x00".join(parts), DRIVER_TAIL

# Comandi di sola lettura (documentati in peripage-python): nome, firmware, batteria.
PRINTER_QUERIES = (("name", "10ff3011"), ("firmware", "10ff20f1"), ("battery", "10ff50f1"))


def parse_printer_reply(key: str, data: bytes):
    """Interpreta la risposta a una domanda: testo per nome/firmware, percentuale (0-100) per la batteria."""
    if not data:
        return None
    if key == "battery":  # risposta di 2 byte: {0, percentuale}
        return int(data[1]) if len(data) >= 2 and data[1] <= 100 else None
    text = data.replace(b"\x00", b"").decode("ascii", errors="replace").strip()
    return text or None


def printer_dpi(firmware):
    """Risoluzione dichiarata nel firmware (es. "V2.11_304dpi" -> 304), se presente."""
    m = re.search(r"(\d{3})\s*dpi", firmware or "", re.IGNORECASE)
    return int(m.group(1)) if m else None


def query_printer(port: str, baud: int = 115200, timeout: float = 1.5) -> dict:
    """Interroga la stampante sulla porta COM con i comandi di sola lettura.

    Ritorna {"name", "firmware", "battery", "dpi"}; i campi senza risposta sono None.
    Se la porta non si apre solleva serial.SerialException.
    """
    import serial  # pyserial

    info = {"name": None, "firmware": None, "battery": None, "dpi": None}
    with serial.Serial(port, baud, timeout=timeout, write_timeout=5) as ser:
        for key, cmd in PRINTER_QUERIES:
            ser.reset_input_buffer()
            ser.write(bytes.fromhex(cmd))
            ser.flush()
            data = ser.read(1)  # attende fino a `timeout` il primo byte
            if data:
                time.sleep(0.15)  # il resto della risposta arriva subito dopo
                data += ser.read(ser.in_waiting)
            info[key] = parse_printer_reply(key, data)
    info["dpi"] = printer_dpi(info["firmware"])
    return info


def _safe_cuts(data: bytes):
    """Offset in cui e' sicuro interrompere l'invio: la fine di ogni comando completo.

    Interrompere a meta' di un comando raster lascerebbe la stampante in attesa dei dati mancanti.
    """
    cuts, i, n = [], 0, len(data)
    if data.startswith(DRIVER_HEAD):
        i = len(DRIVER_HEAD)
        cuts.append(i)
    while i < n:
        if data[i:i + 3] == b"\x1d\x76\x30" and i + 8 <= n:
            i += 8 + data[i + 4] * data[i + 6]  # larghezza in byte * altezza (xH = yH = 0)
        elif data[i:i + 4] == b"\x10\xff\x10\x00":
            i += 5  # cambio di concentration
        else:
            i += 1  # byte 00 di separazione
        cuts.append(min(i, n))
    return cuts


def send_serial(port: str, parts, delay: float, baud: int = 115200, progress=None, cancel=None) -> bool:
    """Invia il flusso a una porta COM (es. la COM virtuale del Bluetooth).

    Come peripage-python, manda i dati a pezzi di una riga (80 byte) con una piccola pausa, per non
    saturare il buffer della stampante. `progress(inviati, totale)` viene chiamata a ogni pezzo;
    se `cancel` (threading.Event) viene impostato, l'invio si ferma alla fine del comando in corso
    e l'ultima parte (la coda che chiude il job) viene comunque mandata.
    Ritorna True se l'invio e' stato interrotto.
    """
    import serial  # pyserial

    total = sum(len(p) for p in parts)
    done = 0
    cancelled = False
    with serial.Serial(port, baud, timeout=5, write_timeout=30) as ser:
        for k, part in enumerate(parts):
            last_part = k == len(parts) - 1
            cuts = [] if last_part else _safe_cuts(part)
            pos, stop_at = 0, None
            while pos < len(part):
                if stop_at is None and not last_part and cancel is not None and cancel.is_set():
                    stop_at = next((c for c in cuts if c >= pos), len(part))
                    cancelled = True
                end = min(pos + DRIVER_ROW_BYTES, len(part))
                if stop_at is not None:
                    end = min(end, stop_at)
                if end > pos:
                    ser.write(part[pos:end])
                    ser.flush()
                    time.sleep(delay)
                    done += end - pos
                    pos = end
                    if progress is not None:
                        progress(done, total)
                if stop_at is not None and pos >= stop_at:
                    break
            time.sleep(0.2)
        time.sleep(0.5)
    return cancelled


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image", nargs="?", help="immagine da stampare (non serve con --ruler)")
    ap.add_argument("--width", type=int, default=PRINT_WIDTH,
                    help="larghezza dell'immagine in punti (default %(default)s = 48 mm a 304 dpi, l'area stampabile; "
                         "massimo 640)")
    ap.add_argument("--status", action="store_true",
                    help="legge nome, firmware, risoluzione e batteria dalla stampante (serve --com)")
    ap.add_argument("--ruler", action="store_true",
                    help="stampa un foglio di prova con righello e quadrati per misurare area stampabile e 304 dpi")
    ap.add_argument("--dither", default="atkinson", choices=METHODS,
                    help="metodo di dithering (default atkinson); per le foto prova stucki, sierra o bluenoise; "
                         "threshold = line art senza dithering")
    ap.add_argument("--no-serpentine", action="store_true", help="scansione sempre da sinistra a destra")
    ap.add_argument("--threshold", type=float, default=128.0,
                    help="con --dither threshold (line art, senza dithering): i pixel piu' scuri di questo valore "
                         "(0-255) diventano neri; alzalo per tenere anche i tratti sottili")
    ap.add_argument("--rotate", type=int, default=0, choices=[0, 90, 180, 270],
                    help="ruota l'immagine di questi gradi in senso orario prima di ridimensionarla")
    ap.add_argument("--gamma", type=float, default=1.0, help="gamma >1 schiarisce (default 1.0)")
    ap.add_argument("--autocontrast", type=float, default=0.0, help="percentuale tagliata agli estremi (es. 1)")
    ap.add_argument("--sharpen", type=float, default=0.0, help="maschera di contrasto in %% (es. 60-120)")
    ap.add_argument("--clarity", type=float, default=0.0, help="contrasto locale (es. 0.3-0.8)")
    ap.add_argument("-c", "--concentration", type=int, choices=[0, 1, 2], default=None,
                    help="annerimento fisso 0/1/2 (default: nessun comando, vale quello corrente della stampante)")
    ap.add_argument("--row-density", action="store_true",
                    help="concentration adattiva per fasce di righe (usa --levels, --band, --smooth, --row-dither)")
    ap.add_argument("--levels", default=",".join(str(v) for v in DEFAULT_LEVELS),
                    help="annerimento relativo a concentration 0,1,2 (default %(default)s)")
    ap.add_argument("--band", type=int, default=2, help="righe per fascia con --row-density (default 2)")
    ap.add_argument("--smooth", type=int, default=3,
                    help="con --row-density: raggio (righe) con cui sfumare i passaggi tra concentration (0 = nessuno)")
    ap.add_argument("--row-dither", default="diffusion", choices=["bluenoise", "diffusion"],
                    help="con --row-density: dithering dei punti (default diffusion)")
    ap.add_argument("--com", help="porta COM (es. COM9) a cui inviare la stampa")
    ap.add_argument("--baud", type=int, default=115200, help="baud rate della porta COM (di solito ignorato)")
    ap.add_argument("--delay", type=float, default=0.01, help="pausa tra le righe inviate (s)")
    ap.add_argument("--dump", help="scrive il flusso in questo file")
    ap.add_argument("--preview", help="salva l'anteprima in questo PNG")
    ap.add_argument("--compare", help="salva un foglio di confronto di tutti i metodi (non stampa)")
    args = ap.parse_args()

    try:
        levels = tuple(float(v) for v in args.levels.split(","))
        assert len(levels) == 3 and all(0.0 < v <= 1.0 for v in levels)
    except (ValueError, AssertionError):
        ap.error("--levels vuole tre numeri tra 0 e 1, es. 0.88,1.0,1.0")
    if args.band < 1:
        ap.error("--band deve essere almeno 1")
    if not args.image and not (args.ruler or args.status):
        ap.error("serve un'immagine oppure --ruler / --status")
    if args.status:
        if not args.com:
            ap.error("--status richiede --com")
        info = query_printer(args.com, args.baud)
        if not any(v is not None for v in info.values()):
            print("Nessuna risposta dalla stampante su", args.com)
            return
        print("Nome:", info["name"] or "-")
        print("Firmware:", info["firmware"] or "-", f"({info['dpi']} dpi)" if info["dpi"] else "")
        print("Batteria:", f"{info['battery']}%" if info["battery"] is not None else "-")
        return
    if not 16 <= args.width <= HEAD_DOTS:
        ap.error(f"--width deve essere tra 16 e {HEAD_DOTS}")

    serpentine = not args.no_serpentine
    if args.ruler:
        burn = ruler_burn()
        print(f"Righello di prova: {burn.shape[1]}x{burn.shape[0]} punti")
        preview = np.where(burn, 0, 255).astype(np.uint8)
        body, tail = driver_stream(burn, args.concentration)
        return finish(args, body, tail, preview)
    gray = load_gray(args.image, args.width, args.gamma, args.autocontrast, args.sharpen, args.clarity, args.rotate)

    if args.compare:
        compare_sheet(gray, args.compare, serpentine)
        print("Foglio di confronto salvato in", args.compare)
        return

    if args.row_density:
        method = args.dither if args.dither in KERNELS else "floyd"
        burn, conc, sim = adaptive_pipeline(gray, levels, method, serpentine, args.band,
                                            args.smooth, args.row_dither)
        print(f"Concentration adattiva: {burn.shape[1]}x{burn.shape[0]} punti, righe a concentration 0/1/2:",
              [int((conc == c).sum()) for c in range(3)])
        preview = (255.0 * (1.0 - sim)).astype(np.uint8)
        body, tail = adaptive_stream(burn, conc)
    else:
        burn = dither_gray(gray, args.dither, serpentine, args.threshold)
        print(f"Immagine pronta: {burn.shape[1]}x{burn.shape[0]} punti, {args.dither}")
        preview = np.where(burn, 0, 255).astype(np.uint8)
        body, tail = driver_stream(burn, args.concentration)

    finish(args, body, tail, preview)


def finish(args, body: bytes, tail: bytes, preview: np.ndarray):
    """Salva l'anteprima e invia (o scrive su file) il flusso."""
    if args.preview:
        Image.fromarray(preview, "L").save(args.preview)
        print("Anteprima salvata in", args.preview)

    if not (args.com or args.dump):
        print("Nessun invio: aggiungi --com COMx per stampare o --dump file per salvare il flusso.")
        return
    if args.dump:
        with open(args.dump, "wb") as f:
            f.write(body + tail)
        print(f"Scritti {len(body) + len(tail)} byte in {args.dump}")
    if args.com:
        send_serial(args.com, [body, tail], args.delay, args.baud)
        print("Stampa inviata a", args.com)


if __name__ == "__main__":
    main()
