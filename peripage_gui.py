#!/usr/bin/env python3
"""
Peripage Print: piccola interfaccia grafica per stampare foto sulla Peripage A6+.
Peripage Print: a small GUI to print photos on the Peripage A6+.

Scegli le foto, la porta COM (la COM virtuale del Bluetooth), il metodo di dithering (o line art
senza dithering per i disegni), il gamma e la concentration; puoi ruotare di 90 gradi le foto
rettangolari. Il programma ridimensiona a 640 punti, fa il dithering e invia i dati.
L'interfaccia e' in italiano o in inglese (selettore "Lingua / Language").
Usa peripage_dither.py (deve stare nella stessa cartella).

Trascinamento dei file sulla finestra / drag and drop: pip install tkinterdnd2 (facoltativo / optional).

Exe per Windows / Windows exe:
  pip install pillow numpy pyserial tkinterdnd2 pyinstaller
  pyinstaller --onefile --windowed --name PeripagePrint --collect-all tkinterdnd2 --hidden-import serial.tools.list_ports_windows peripage_gui.py
"""

import json
import locale
import os
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import numpy as np
from PIL import Image, ImageTk
from serial.tools import list_ports

try:  # trascinamento dei file sulla finestra (pip install tkinterdnd2); senza, l'app funziona lo stesso
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:
    DND_FILES = TkinterDnD = None

import peripage_dither as pd

APP_NAME = "Peripage Print"
DEFAULT_WIDTH = pd.PRINT_WIDTH   # 576 punti = 48 mm a 304 dpi: l'area realmente stampata
MIN_WIDTH, MAX_WIDTH = 200, pd.HEAD_DOTS
TAIL_FEED_ROWS = 60              # righe bianche in coda (circa 10 mm) per poter strappare la carta
PAUSE_BETWEEN_JOBS = 5.0         # secondi di attesa tra una foto e la successiva
SEND_DELAY = 0.012               # pausa tra le righe inviate (come nello script)
BAUD = 115200                    # la COM virtuale del Bluetooth di solito lo ignora
PREVIEW_DELAY_MS = 400           # attesa dopo l'ultima modifica prima di ricalcolare l'anteprima
DOTS_PER_CM = 304 / 2.54         # la A6+ stampa a 304 dpi

LINEART = "lineart"              # identificativo interno del metodo "line art"
METHOD_IDS = [LINEART] + [m for m in pd.METHODS if m != "threshold"]
CONC_IDS = ["1", "0", "2", "none"]
DEFAULT_THRESHOLD = 160
IMAGE_EXT = "*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp"
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp", ".gif")
LOW_BATTERY = 15
LANGS = {"it": "Italiano", "en": "English"}

# ---------------------------------------------------------------- testi / texts
STRINGS = {
    "it": {
        "tab_image": "Immagine", "tab_printer": "Stampante", "reduced": "ridotto al {p}%",
        "tip_compare": "Mostra l'immagine di partenza accanto al risultato. Se nella finestra non c'è spazio, le due "
                       "metà vengono ridotte alla stessa scala: così si vede quanto il dithering rende i toni. "
                       "Per vedere i punti 1:1 disattiva il confronto.",
        "sharpen": "Nitidezza (?)", "clarity": "Contrasto locale (?)", "autocontrast": "Auto-contrasto (?)",
        "compare": "Affianca originale", "orig": "Originale", "result": "Risultato",
        "printer_status": "Stato stampante", "checking": "Interrogo la stampante su {port}…",
        "status_done": "Stato della stampante letto.", "status_nodata": "Nessuna risposta dalla stampante.",
        "st_name": "Nome", "st_fw": "Firmware", "st_dpi": "Risoluzione", "st_batt": "Batteria", "st_low": "bassa",
        "st_none": "Nessuna risposta dalla stampante su {port}. Controlla che sia accesa; con il Bluetooth "
                   "Windows crea spesso due porte COM (in uscita e in ingresso): prova l'altra.",
        "st_fail": "Impossibile aprire {port}: {msg}",
        "no_ports": "Nessuna porta COM trovata. Accoppia la stampante in Bluetooth e premi ↻.",
        "ports_found": "Trovate {n} porte COM.", "ports_found_1": "Trovata 1 porta COM.",
        "hint_add_dnd": "Aggiungi o trascina qui una o più foto.",
        "drop_none": "Nessuna immagine tra i file trascinati.",
        "tip_sharpen": "Accentua i bordi (maschera di contrasto). I punti della stampante ammorbidiscono i "
                       "dettagli, quindi sulle foto 60–120% di solito aiuta; con valori alti compaiono aloni.\n"
                       "Doppio clic per azzerare.",
        "tip_clarity": "Contrasto locale: esalta i dettagli rispetto allo sfondo sfocato senza bruciare luci e "
                       "ombre. 0,3–0,8 per foto piatte o nebbiose.\nDoppio clic per azzerare.",
        "tip_autocontrast": "Usa tutta la gamma dal nero al bianco, ignorando la percentuale indicata di pixel più "
                            "scuri e più chiari. 1% è un buon punto di partenza per foto «grigie» o scansioni.\n"
                            "Doppio clic per azzerare.",
        "tip_status": "Legge dalla stampante nome, firmware (con la risoluzione dichiarata) e batteria, con "
                      "comandi di sola lettura. Serve anche per capire quale porta COM è quella giusta: "
                      "risponde solo quella collegata alla stampante.",
        "tip_com": "Le porte COM presenti in questo momento, lette da Windows (non sono predefinite). "
                   "↻ aggiorna l'elenco, che si aggiorna anche quando apri il menu.\n"
                   "Con il Bluetooth compaiono spesso due porte: usa quella che risponde a «Stato stampante».",
        "width": "Larghezza (punti) (?)", "ruler": "Stampa righello di prova", "ruler_name": "righello di prova",
        "photos": "Foto", "add": "Aggiungi…", "remove": "Rimuovi", "clear": "Svuota",
        "settings": "Impostazioni", "com": "Porta COM", "dither": "Dithering (?)", "threshold": "Soglia (?)",
        "gamma": "Gamma (?)", "conc": "Concentrazione (?)", "language": "Lingua / Language",
        "print": "Stampa", "print_1": "Stampa (1 foto)", "print_n": "Stampa ({n} foto)", "cancel": "Annulla",
        "lineart": "line art (senza dither)", "conc_none": "non impostare",
        "preview_title": "Anteprima (come verrà stampata, 1:1)",
        "preview_title_name": "Anteprima: {name} (come verrà stampata, 1:1)",
        "hint_add": "Aggiungi una o più foto.",
        "calc_preview": "Calcolo l'anteprima…",
        "preview_info": "Anteprima: {w}×{h} punti, circa {cm} cm di carta.",
        "preview_failed": "Anteprima non riuscita: {msg}",
        "preparing": "[{i}/{n}] Preparo {name}…",
        "sending": "[{i}/{n}] Invio {name} a {com}…",
        "waiting": "[{i}/{n}] Attendo la stampante… {s} s",
        "cancelling": "Annullo alla fine del blocco in corso…",
        "cancelled": "Annullato.",
        "done": "Fatto: {n} foto inviate.", "done_1": "Fatto: 1 foto inviata.", "done_ruler": "Righello di prova inviato.",
        "print_error": "Errore durante la stampa.",
        "error_hint": "{msg}\n\nControlla che la stampante sia accesa e accoppiata, che la porta COM sia quella "
                      "giusta e che nessun altro programma la stia usando.",
        "need_photo": "Aggiungi almeno una foto.",
        "need_com": "Scegli la porta COM della stampante.",
        "confirm_quit": "Una stampa è in corso. Vuoi interromperla e chiudere?",
        "choose_photos": "Scegli le foto", "ft_images": "Immagini", "ft_all": "Tutti i file",
        "tip_dither": "Come i grigi diventano punti neri.\n"
                      "• stucki, sierra, jarvis: foto dettagliate e morbide\n"
                      "• atkinson: più contrasto, ombre e luci pulite\n"
                      "• floyd: il classico\n"
                      "• bluenoise: punti uniformi; bayer: motivo a griglia\n"
                      "• line art: nessun dithering, solo bianco e nero netto, per disegni al tratto.",
        "tip_threshold": "Solo per line art. I pixel più scuri di questo valore diventano neri.\n"
                         "Alza per tenere anche i tratti sottili o chiari (le linee diventano un po' più spesse), "
                         "abbassa per tenere solo il nero pieno.",
        "tip_gamma": "Schiarisce o scurisce i mezzitoni prima del dithering.\n"
                     "Sopra 1 schiarisce (1,2–1,5 di solito va bene, perché la carta termica tende a scurire), "
                     "sotto 1 scurisce. Non cambia il bianco e il nero puri. Con line art non viene usato.",
        "tip_conc": "Energia con cui la testina brucia i punti.\n"
                    "0 = leggera, 1 = media, 2 = massima. Sui punti del dithering 1 e 2 danno quasi lo stesso "
                    "risultato, mentre sulle aree piene 2 è molto più nera; 0 è un po' più chiara.\n"
                    "«non impostare» lascia il valore corrente della stampante; il valore scelto potrebbe restare "
                    "attivo finché non spegni la stampante.",
        "tip_rotate": "Ruota di 90° le foto selezionate (o quella in anteprima).\n"
                      "Una foto orizzontale stampata in verticale viene più grande: il lato corto occupa "
                      "tutta la larghezza della carta.",
        "tip_print": "Stampa le foto selezionate nell'elenco; se non ne selezioni nessuna, le stampa tutte.\n"
                     "L'anteprima mostra l'ultima foto su cui hai cliccato.",
        "tip_width": "Larghezza dell'immagine stampata, in punti (304 punti = 1 pollice = 25,4 mm).\n"
                     "L'area stampabile della A6+ è di circa 48 mm, cioè 576 punti: oltre questa larghezza "
                     "la testina non stampa e il bordo destro viene tagliato. Se anche con 576 perdi un po' "
                     "di bordo, riduci il valore; il righello di prova serve a misurare.",
        "tip_ruler": "Stampa un foglio di prova con un righello, una cornice e due quadrati di 25,4 mm e "
                     "12,7 mm (304 e 152 punti).\nMisurali con un righello per controllare l'area stampabile "
                     "e la risoluzione (304 punti per pollice).",
    },
    "en": {
        "tab_image": "Image", "tab_printer": "Printer", "reduced": "reduced to {p}%",
        "tip_compare": "Shows the source image next to the result. If the window is too narrow, both halves are "
                       "reduced to the same scale: this shows how well the dithering renders the tones. "
                       "Turn the comparison off to see the dots at 1:1.",
        "sharpen": "Sharpness (?)", "clarity": "Local contrast (?)", "autocontrast": "Auto-contrast (?)",
        "compare": "Show original side by side", "orig": "Original", "result": "Result",
        "printer_status": "Printer status", "checking": "Querying the printer on {port}…",
        "status_done": "Printer status read.", "status_nodata": "No reply from the printer.",
        "st_name": "Name", "st_fw": "Firmware", "st_dpi": "Resolution", "st_batt": "Battery", "st_low": "low",
        "st_none": "No reply from the printer on {port}. Make sure it is on; with Bluetooth Windows often "
                   "creates two COM ports (outgoing and incoming): try the other one.",
        "st_fail": "Cannot open {port}: {msg}",
        "no_ports": "No COM ports found. Pair the printer over Bluetooth and press ↻.",
        "ports_found": "{n} COM ports found.", "ports_found_1": "1 COM port found.",
        "hint_add_dnd": "Add or drop one or more photos here.",
        "drop_none": "No images among the dropped files.",
        "tip_sharpen": "Sharpens edges (unsharp mask). The printer's dots soften details, so on photos 60–120% "
                       "usually helps; high values create halos.\nDouble-click to reset.",
        "tip_clarity": "Local contrast: boosts details against the blurred background without burning highlights "
                       "and shadows. 0.3–0.8 for flat or hazy photos.\nDouble-click to reset.",
        "tip_autocontrast": "Uses the whole range from black to white, ignoring the given percentage of darkest "
                            "and lightest pixels. 1% is a good starting point for \"gray\" photos or scans.\n"
                            "Double-click to reset.",
        "tip_status": "Reads the printer's name, firmware (with the declared resolution) and battery using "
                      "read-only commands. Also useful to find the right COM port: only the one connected to "
                      "the printer replies.",
        "tip_com": "The COM ports present right now, read from Windows (they are not hard-coded). "
                   "↻ refreshes the list, which also refreshes when you open the menu.\n"
                   "With Bluetooth there are often two ports: use the one that answers to \"Printer status\".",
        "width": "Width (dots) (?)", "ruler": "Print test ruler", "ruler_name": "test ruler",
        "photos": "Photos", "add": "Add…", "remove": "Remove", "clear": "Clear",
        "settings": "Settings", "com": "COM port", "dither": "Dithering (?)", "threshold": "Threshold (?)",
        "gamma": "Gamma (?)", "conc": "Concentration (?)", "language": "Lingua / Language",
        "print": "Print", "print_1": "Print (1 photo)", "print_n": "Print ({n} photos)", "cancel": "Cancel",
        "lineart": "line art (no dither)", "conc_none": "don't set",
        "preview_title": "Preview (as it will be printed, 1:1)",
        "preview_title_name": "Preview: {name} (as it will be printed, 1:1)",
        "hint_add": "Add one or more photos.",
        "calc_preview": "Computing preview…",
        "preview_info": "Preview: {w}×{h} dots, about {cm} cm of paper.",
        "preview_failed": "Preview failed: {msg}",
        "preparing": "[{i}/{n}] Preparing {name}…",
        "sending": "[{i}/{n}] Sending {name} to {com}…",
        "waiting": "[{i}/{n}] Waiting for the printer… {s} s",
        "cancelling": "Cancelling at the end of the current block…",
        "cancelled": "Cancelled.",
        "done": "Done: {n} photos sent.", "done_1": "Done: 1 photo sent.", "done_ruler": "Test ruler sent.",
        "print_error": "Printing error.",
        "error_hint": "{msg}\n\nMake sure the printer is on and paired, that the COM port is the right one "
                      "and that no other program is using it.",
        "need_photo": "Add at least one photo.",
        "need_com": "Choose the printer's COM port.",
        "confirm_quit": "A print is in progress. Do you want to stop it and quit?",
        "choose_photos": "Choose photos", "ft_images": "Images", "ft_all": "All files",
        "tip_dither": "How grays are turned into black dots.\n"
                      "• stucki, sierra, jarvis: detailed, smooth photos\n"
                      "• atkinson: more contrast, clean shadows and highlights\n"
                      "• floyd: the classic\n"
                      "• bluenoise: uniform dots; bayer: grid-like pattern\n"
                      "• line art: no dithering, just crisp black and white, for line drawings.",
        "tip_threshold": "Line art only. Pixels darker than this value become black.\n"
                         "Raise it to keep thin or light strokes (lines get a bit thicker), "
                         "lower it to keep only solid black.",
        "tip_gamma": "Lightens or darkens the midtones before dithering.\n"
                     "Above 1 it lightens (1.2–1.5 is usually good, since thermal paper tends to print dark), "
                     "below 1 it darkens. Pure white and pure black do not change. Not used with line art.",
        "tip_conc": "Energy the print head uses to burn the dots.\n"
                    "0 = light, 1 = medium, 2 = maximum. On dithered dots 1 and 2 give almost the same result, "
                    "while on solid areas 2 is much blacker; 0 is slightly lighter.\n"
                    "\"Don't set\" leaves the printer's current value; the value you choose may stay active "
                    "until you switch the printer off.",
        "tip_rotate": "Rotates the selected photos by 90° (or the previewed one if none is selected).\n"
                      "A landscape photo printed in portrait comes out larger: its short side fills the "
                      "whole paper width.",
        "tip_print": "Prints the photos selected in the list; if none is selected, it prints them all.\n"
                     "The preview shows the photo you clicked last.",
        "tip_width": "Width of the printed image, in dots (304 dots = 1 inch = 25.4 mm).\n"
                     "The A6+ printable area is about 48 mm, i.e. 576 dots: beyond that width the print head "
                     "does not print and the right edge is cut off. If you still lose some edge at 576, lower "
                     "the value; the test ruler helps you measure it.",
        "tip_ruler": "Prints a test sheet with a ruler, a frame and two squares of 25.4 mm and 12.7 mm "
                     "(304 and 152 dots).\nMeasure them with a ruler to check the printable area and the "
                     "resolution (304 dots per inch).",
    },
}


def detect_language() -> str:
    """Lingua dell'interfaccia di Windows (italiano se e' italiano, altrimenti inglese)."""
    try:
        import ctypes
        langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        return "it" if (langid & 0x3FF) == 0x10 else "en"
    except (ImportError, AttributeError, OSError):
        pass
    try:
        code = (locale.getlocale()[0] or "").lower()
    except (ValueError, TypeError):
        code = ""
    return "it" if code.startswith("it") else "en"


def settings_path() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "PeripagePrint", "settings.json")


def load_settings() -> dict:
    try:
        with open(settings_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(data: dict):
    try:
        os.makedirs(os.path.dirname(settings_path()), exist_ok=True)
        with open(settings_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass


def render(path: str, method: str, gamma: float, threshold: int = DEFAULT_THRESHOLD, rotate: int = 0,
           width: int = DEFAULT_WIDTH, adjust=(0, 0.0, 0.0)):
    """Ritorna (gray, burn): toni di grigio dopo le regolazioni e punti da stampare.

    adjust = (nitidezza %, contrasto locale, auto-contrasto %)."""
    sharpen, clarity, autoc = adjust
    if method == LINEART:  # nessun dithering e nessun gamma: solo soglia
        gray = pd.load_gray(path, width, 1.0, autoc, sharpen, clarity, rotate)
        return gray, pd.dither_gray(gray, "threshold", True, float(threshold))
    gray = pd.load_gray(path, width, gamma, autoc, sharpen, clarity, rotate)
    return gray, pd.dither_gray(gray, method, True)


def make_burn(path: str, method: str, gamma: float, threshold: int = DEFAULT_THRESHOLD,
              rotate: int = 0, width: int = DEFAULT_WIDTH, adjust=(0, 0.0, 0.0)) -> np.ndarray:
    return render(path, method, gamma, threshold, rotate, width, adjust)[1]


def port_sort_key(port):
    """Ordine naturale: le porte con il numero piu' piccolo prima (2 prima di 10)."""
    m = re.search(r"(\d+)$", port.device)
    return (port.device.rstrip("0123456789"), int(m.group(1)) if m else 0)


class Tooltip:
    """Nuvoletta di spiegazione che compare dopo mezzo secondo con il mouse fermo sul widget.
    `text` puo' essere una stringa o una funzione (cosi' segue il cambio di lingua)."""

    def __init__(self, widget, text, delay: int = 500):
        self.widget, self.text, self.delay = widget, text, delay
        self.after_id = None
        self.window = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None):
        self._hide()
        self.after_id = self.widget.after(self.delay, self._show)

    def _show(self):
        self.after_id = None
        text = self.text() if callable(self.text) else self.text
        w = tk.Toplevel(self.widget)
        w.wm_overrideredirect(True)
        tk.Label(w, text=text, justify="left", background="#ffffe1", relief="solid", borderwidth=1,
                 wraplength=340, padx=8, pady=6).pack()
        w.update_idletasks()
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        x = max(0, min(x, self.widget.winfo_screenwidth() - w.winfo_reqwidth() - 10))
        if y + w.winfo_reqheight() > self.widget.winfo_screenheight():
            y = self.widget.winfo_rooty() - w.winfo_reqheight() - 6
        w.wm_geometry(f"+{x}+{y}")
        self.window = w

    def _hide(self, _event=None):
        if self.after_id is not None:
            self.widget.after_cancel(self.after_id)
            self.after_id = None
        if self.window is not None:
            self.window.destroy()
            self.window = None


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(APP_NAME)
        w = min(1400, max(900, root.winfo_screenwidth() - 60))
        h = min(800, max(700, root.winfo_screenheight() - 100))
        root.geometry(f"{w}x{h}")
        root.minsize(900, 700)
        self.settings = load_settings()
        lang = self.settings.get("lang")
        self.lang = lang if lang in LANGS else detect_language()
        self.paths = []
        self.rot = {}                # percorso -> rotazione in gradi (senso orario)
        self.preview_index = None    # foto mostrata nell'anteprima (indipendente dalla selezione)
        self.preview_name = None
        self.dnd = TkinterDnD is not None and hasattr(root, "drop_target_register")
        self.status_msg = ("hint_add_dnd" if self.dnd else "hint_add", {})
        self.querying = False        # lettura dello stato della stampante in corso
        self.render_cache = None     # ultima anteprima calcolata: (nome, grigi, punti)
        self.last_canvas_w = 0
        self.resize_after = None
        self.printer_result = None   # ultimo esito di "Stato stampante"
        self.q = queue.Queue()
        self.cancel = threading.Event()
        self.worker = None
        self.preview_after = None
        self.preview_seq = 0
        self.preview_photo = None
        self.ports = []
        # impostazioni salvate (accetta anche i valori delle versioni precedenti)
        saved = self.settings.get("dither", "stucki")
        self.method_id = LINEART if saved in (LINEART, "line art (senza dither)") else (
            saved if saved in METHOD_IDS else "stucki")
        saved = str(self.settings.get("conc", "1"))
        self.conc_id = "none" if saved in ("none", "non impostare") else (saved if saved in CONC_IDS else "1")
        try:
            self.last_width = max(MIN_WIDTH, min(MAX_WIDTH, int(self.settings.get("width", DEFAULT_WIDTH))))
        except (TypeError, ValueError):
            self.last_width = DEFAULT_WIDTH
        self._build()
        self.setup_dnd()
        self.retranslate()
        self.refresh_ports()
        self.on_method(schedule=False)
        self.root.after(100, self.poll)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------- testi ----------
    def t(self, key: str, **params) -> str:
        text = STRINGS[self.lang][key]
        params = {k: (f"{v:.1f}".replace(".", ",") if (isinstance(v, float) and self.lang == "it")
                      else (f"{v:.1f}" if isinstance(v, float) else v)) for k, v in params.items()}
        return text.format(**params) if params else text

    def tip(self, key: str):
        return lambda: self.t(key)

    def set_status(self, key: str, **params):
        self.status_msg = (key, params)
        self.status.set(self.t(key, **params))

    def retranslate(self):
        """Applica la lingua corrente a tutti i testi dell'interfaccia."""
        t = self.t
        self.photos_frame.configure(text=t("photos"))
        self.add_btn.configure(text=t("add"))
        self.remove_btn.configure(text=t("remove"))
        self.clear_btn.configure(text=t("clear"))
        self.nb.tab(self.tab_image, text=t("tab_image"))
        self.nb.tab(self.tab_printer, text=t("tab_printer"))
        self.dither_title.configure(text=t("dither"))
        self.thr_title.configure(text=t("threshold"))
        self.gamma_title.configure(text=t("gamma"))
        self.sharp_title.configure(text=t("sharpen"))
        self.clar_title.configure(text=t("clarity"))
        self.auto_title.configure(text=t("autocontrast"))
        self.com_title.configure(text=t("com"))
        self.conc_title.configure(text=t("conc"))
        self.width_title.configure(text=t("width"))
        self.status_btn.configure(text=t("printer_status"))
        self.ruler_btn.configure(text=t("ruler"))
        self.lang_title.configure(text=t("language"))
        self.cancel_btn.configure(text=t("cancel"))
        self.compare_chk.configure(text=t("compare"))
        self.method_box.configure(values=[t("lineart") if m == LINEART else m for m in METHOD_IDS])
        self.method_box.current(METHOD_IDS.index(self.method_id))
        self.conc_box.configure(values=[t("conc_none") if c == "none" else c for c in CONC_IDS])
        self.conc_box.current(CONC_IDS.index(self.conc_id))
        self.lang_box.current(list(LANGS).index(self.lang))
        self.update_print_label()
        self.update_preview_title()
        self.show_printer_result()
        if self.render_cache:
            self.show_preview(*self.render_cache, quiet=True)
        self.status.set(self.t(self.status_msg[0], **self.status_msg[1]))

    def on_language(self, _event=None):
        self.lang = list(LANGS)[self.lang_box.current()]
        self.retranslate()

    # ---------- interfaccia ----------
    def _slider(self, parent, row, var, lo, hi, tip_key):
        title = ttk.Label(parent)
        title.grid(row=row, column=0, sticky="w", pady=3)
        scale = ttk.Scale(parent, from_=lo, to=hi, variable=var, command=self.on_adjust)
        scale.grid(row=row, column=1, sticky="ew", pady=3, padx=(6, 0))
        value = ttk.Label(parent, width=6, anchor="e")
        value.grid(row=row, column=2, padx=(4, 0))
        for w in (title, scale):
            Tooltip(w, self.tip(tip_key))
        return title, scale, value

    def _reset_on_double_click(self, scale, var):
        def handler(_event):
            var.set(0)
            self.on_adjust()
            return "break"  # evita che il clic sposti di nuovo il cursore
        scale.bind("<Double-Button-1>", handler)

    def _build(self):
        root = self.root
        st = self.settings
        left = ttk.Frame(root, padding=10)
        left.pack(side="left", fill="y")
        right = ttk.Frame(root, padding=(0, 10, 10, 10))
        right.pack(side="left", fill="both", expand=True)

        # Foto
        self.photos_frame = ttk.LabelFrame(left, padding=8)
        self.photos_frame.pack(fill="both", expand=True)
        box = ttk.Frame(self.photos_frame)
        box.pack(fill="both", expand=True)
        # exportselection=False: la selezione non sparisce quando clicchi altri controlli
        self.listbox = tk.Listbox(box, width=36, height=8, selectmode="extended", activestyle="none",
                                  exportselection=False)
        sb = ttk.Scrollbar(box, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=sb.set)
        self.listbox.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.listbox.bind("<<ListboxSelect>>", self.on_select)
        row = ttk.Frame(self.photos_frame)
        row.pack(fill="x", pady=(6, 0))
        self.add_btn = ttk.Button(row, command=self.add_photos)
        self.remove_btn = ttk.Button(row, command=self.remove_photos)
        self.clear_btn = ttk.Button(row, command=self.clear_photos)
        self.add_btn.pack(side="left")
        self.remove_btn.pack(side="left", padx=4)
        self.clear_btn.pack(side="left")
        row2 = ttk.Frame(self.photos_frame)
        row2.pack(fill="x", pady=(6, 0))
        rot_left = ttk.Button(row2, text="↺ 90°", width=8, command=lambda: self.rotate(-90))
        rot_right = ttk.Button(row2, text="↻ 90°", width=8, command=lambda: self.rotate(90))
        rot_left.pack(side="left")
        rot_right.pack(side="left", padx=4)
        for b in (rot_left, rot_right):
            Tooltip(b, self.tip("tip_rotate"))

        # Schede: Immagine / Stampante
        self.nb = ttk.Notebook(left)
        self.nb.pack(fill="x", pady=(10, 0))
        self.tab_image = ttk.Frame(self.nb, padding=8)
        self.tab_printer = ttk.Frame(self.nb, padding=8)
        self.nb.add(self.tab_image, text="-")
        self.nb.add(self.tab_printer, text="-")

        # --- scheda Immagine ---
        opts = self.tab_image
        opts.columnconfigure(1, weight=1)
        self.dither_title = ttk.Label(opts)
        self.dither_title.grid(row=0, column=0, sticky="w", pady=3)
        self.method_box = ttk.Combobox(opts, state="readonly", width=24)
        self.method_box.grid(row=0, column=1, columnspan=2, sticky="ew", pady=3, padx=(6, 0))
        self.method_box.bind("<<ComboboxSelected>>", self.on_method_selected)
        for w in (self.dither_title, self.method_box):
            Tooltip(w, self.tip("tip_dither"))

        self.thr_var = tk.DoubleVar(value=float(st.get("threshold", DEFAULT_THRESHOLD)))
        self.gamma_var = tk.DoubleVar(value=float(st.get("gamma", 1.2)))
        self.sharp_var = tk.DoubleVar(value=float(st.get("sharpen", 0)))
        self.clar_var = tk.DoubleVar(value=float(st.get("clarity", 0)))
        self.auto_var = tk.DoubleVar(value=float(st.get("autocontrast", 0)))
        self.thr_title, self.thr_scale, self.thr_label = self._slider(opts, 1, self.thr_var, 0, 255, "tip_threshold")
        self.gamma_title, self.gamma_scale, self.gamma_label = self._slider(opts, 2, self.gamma_var, 0.5, 2.5,
                                                                            "tip_gamma")
        self.sharp_title, self.sharp_scale, self.sharp_label = self._slider(opts, 3, self.sharp_var, 0, 200,
                                                                            "tip_sharpen")
        self.clar_title, self.clar_scale, self.clar_label = self._slider(opts, 4, self.clar_var, 0, 1.5,
                                                                         "tip_clarity")
        self.auto_title, self.auto_scale, self.auto_label = self._slider(opts, 5, self.auto_var, 0, 5,
                                                                         "tip_autocontrast")
        for scale, var in ((self.sharp_scale, self.sharp_var), (self.clar_scale, self.clar_var),
                           (self.auto_scale, self.auto_var)):
            self._reset_on_double_click(scale, var)
        self.on_adjust(None, schedule=False)

        # --- scheda Stampante ---
        prt = self.tab_printer
        prt.columnconfigure(1, weight=1)
        self.com_title = ttk.Label(prt)
        self.com_title.grid(row=0, column=0, sticky="w", pady=3)
        self.com_var = tk.StringVar()
        # postcommand: l'elenco si rilegge da Windows ogni volta che apri il menu
        self.com_box = ttk.Combobox(prt, textvariable=self.com_var, state="readonly", width=26,
                                    postcommand=self.refresh_ports)
        self.com_box.grid(row=0, column=1, sticky="ew", pady=3, padx=(6, 0))
        ttk.Button(prt, text="↻", width=3, command=lambda: self.refresh_ports(announce=True)).grid(
            row=0, column=2, padx=(4, 0))
        for w in (self.com_title, self.com_box):
            Tooltip(w, self.tip("tip_com"))

        self.conc_title = ttk.Label(prt)
        self.conc_title.grid(row=1, column=0, sticky="w", pady=3)
        self.conc_box = ttk.Combobox(prt, state="readonly")
        self.conc_box.grid(row=1, column=1, columnspan=2, sticky="ew", pady=3, padx=(6, 0))
        self.conc_box.bind("<<ComboboxSelected>>", self.on_conc_selected)
        for w in (self.conc_title, self.conc_box):
            Tooltip(w, self.tip("tip_conc"))

        self.width_title = ttk.Label(prt)
        self.width_title.grid(row=2, column=0, sticky="w", pady=3)
        self.width_var = tk.StringVar(value=str(self.last_width))
        self.width_spin = ttk.Spinbox(prt, from_=MIN_WIDTH, to=MAX_WIDTH, increment=8, width=7,
                                      textvariable=self.width_var, command=self.on_width)
        self.width_spin.grid(row=2, column=1, sticky="w", pady=3, padx=(6, 0))
        self.width_spin.bind("<Return>", self.on_width)
        self.width_spin.bind("<FocusOut>", self.on_width)
        for w in (self.width_title, self.width_spin):
            Tooltip(w, self.tip("tip_width"))

        self.status_btn = ttk.Button(prt, command=self.check_printer)
        self.status_btn.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        Tooltip(self.status_btn, self.tip("tip_status"))
        self.status_info = tk.StringVar()
        ttk.Label(prt, textvariable=self.status_info, wraplength=300, justify="left").grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(2, 0))
        self.ruler_btn = ttk.Button(prt, command=self.start_ruler)
        self.ruler_btn.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(6, 3))
        Tooltip(self.ruler_btn, self.tip("tip_ruler"))

        self.lang_title = ttk.Label(prt)
        self.lang_title.grid(row=6, column=0, sticky="w", pady=(8, 3))
        self.lang_box = ttk.Combobox(prt, state="readonly", values=list(LANGS.values()))
        self.lang_box.grid(row=6, column=1, columnspan=2, sticky="ew", pady=(8, 3), padx=(6, 0))
        self.lang_box.bind("<<ComboboxSelected>>", self.on_language)

        # Stampa
        act = ttk.Frame(left)
        act.pack(fill="x", pady=(10, 0))
        self.print_btn = ttk.Button(act, command=self.start_print)
        self.print_btn.pack(side="left", fill="x", expand=True)
        Tooltip(self.print_btn, self.tip("tip_print"))
        self.cancel_btn = ttk.Button(act, command=self.cancel_print, state="disabled")
        self.cancel_btn.pack(side="left", padx=(6, 0))
        self.progress = ttk.Progressbar(left, maximum=100)
        self.progress.pack(fill="x", pady=(8, 0))
        self.status = tk.StringVar()
        ttk.Label(left, textvariable=self.status, wraplength=330, justify="left").pack(fill="x", pady=(6, 0))

        # Anteprima
        head = ttk.Frame(right)
        head.pack(fill="x")
        self.preview_title = tk.StringVar()
        ttk.Label(head, textvariable=self.preview_title).pack(side="left")
        self.compare_var = tk.BooleanVar(value=bool(st.get("compare", False)))
        self.compare_chk = ttk.Checkbutton(head, variable=self.compare_var, command=self.on_compare)
        self.compare_chk.pack(side="right")
        Tooltip(self.compare_chk, self.tip("tip_compare"))
        frame = ttk.Frame(right)
        frame.pack(fill="both", expand=True, pady=(4, 0))
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(frame, background="#d8d8d8", highlightthickness=0)
        vs = ttk.Scrollbar(frame, orient="vertical", command=self.canvas.yview)
        hs = ttk.Scrollbar(frame, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        hs.grid(row=1, column=0, sticky="ew")
        self.canvas.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(-1 * (e.delta // 120), "units"))
        self.canvas.bind("<Configure>", self.on_canvas_resize)
        self.canvas.bind("<Shift-MouseWheel>", lambda e: self.canvas.xview_scroll(-1 * (e.delta // 120), "units"))

    def update_preview_title(self):
        if self.preview_name:
            self.preview_title.set(self.t("preview_title_name", name=self.preview_name))
        else:
            self.preview_title.set(self.t("preview_title"))

    def on_adjust(self, _value=None, schedule=True):
        """Aggiorna i valori mostrati accanto ai cursori e ricalcola l'anteprima."""
        self.thr_label.configure(text=f"{int(round(self.thr_var.get()))}")
        self.gamma_label.configure(text=f"{self.gamma_var.get():.2f}")
        self.sharp_label.configure(text=f"{int(round(self.sharp_var.get()))}%")
        self.clar_label.configure(text=f"{self.clar_var.get():.2f}")
        self.auto_label.configure(text=f"{self.auto_var.get():.1f}%")
        if schedule:
            self.schedule_preview()

    def on_gamma(self, _value=None, schedule=True):
        self.on_adjust(schedule=schedule)

    def on_threshold(self, _value=None, schedule=True):
        self.on_adjust(schedule=schedule)

    def get_adjust(self):
        """(nitidezza %, contrasto locale, auto-contrasto %) per le funzioni di elaborazione."""
        return (int(round(self.sharp_var.get())), round(self.clar_var.get(), 2), round(self.auto_var.get(), 1))

    def get_width(self) -> int:
        """Larghezza in punti, limitata a [MIN_WIDTH, MAX_WIDTH]; se il campo non e' valido usa l'ultima buona."""
        try:
            v = int(float(str(self.width_var.get()).replace(",", ".")))
        except (ValueError, tk.TclError):
            v = self.last_width
        self.last_width = max(MIN_WIDTH, min(MAX_WIDTH, v))
        return self.last_width

    def on_width(self, _event=None):
        before = self.width_var.get()
        v = self.get_width()
        if str(v) != before:
            self.width_var.set(str(v))
        self.schedule_preview()

    def on_method_selected(self, _event=None):
        self.method_id = METHOD_IDS[self.method_box.current()]
        self.on_method()

    def on_conc_selected(self, _event=None):
        self.conc_id = CONC_IDS[self.conc_box.current()]

    def set_method(self, method_id: str):
        self.method_id = method_id
        self.method_box.current(METHOD_IDS.index(method_id))
        self.on_method()

    def on_method(self, schedule=True):
        """Soglia attiva solo per line art; gamma attivo solo per i metodi con dithering."""
        lineart = self.method_id == LINEART
        self.thr_scale.state(["!disabled"] if lineart else ["disabled"])
        self.gamma_scale.state(["disabled"] if lineart else ["!disabled"])
        if schedule:
            self.schedule_preview()

    # ---------- porte COM ----------
    def refresh_ports(self, announce: bool = False):
        """Rilegge da Windows le porte COM presenti (nessuna e' predefinita)."""
        self.ports = sorted(list_ports.comports(), key=port_sort_key)
        labels = [f"{p.device} — {p.description}" for p in self.ports]
        devices = [p.device for p in self.ports]
        self.com_box.configure(values=labels)
        pick = None
        for dev in (self.selected_port(), self.settings.get("com")):
            if dev in devices:
                pick = labels[devices.index(dev)]
                break
        if pick is None and len(labels) == 1:
            pick = labels[0]
        self.com_var.set(pick or "")
        if announce and self.worker is None and not self.querying:
            if not labels:
                self.set_status("no_ports")
            else:
                self.set_status("ports_found_1" if len(labels) == 1 else "ports_found", n=len(labels))

    def selected_port(self) -> str:
        return self.com_var.get().split(" — ")[0].strip()

    # ---------- elenco foto ----------
    def label_for(self, path: str) -> str:
        deg = self.rot.get(path, 0)
        return os.path.basename(path) + (f"   [{deg}°]" if deg else "")

    def refresh_label(self, i: int):
        selected = i in self.listbox.curselection()
        self.listbox.delete(i)
        self.listbox.insert(i, self.label_for(self.paths[i]))
        if selected:
            self.listbox.selection_set(i)

    def update_print_label(self):
        n = len(self.listbox.curselection()) or len(self.paths)
        self.print_btn.configure(text=self.t("print") if n == 0 else
                                 (self.t("print_1") if n == 1 else self.t("print_n", n=n)))

    def set_preview_index(self, idx):
        """Imposta la foto dell'anteprima e la evidenzia (senza toccare le altre selezionate)."""
        self.preview_index = idx
        if idx is not None:
            self.listbox.selection_anchor(idx)
            self.listbox.see(idx)

    def on_select(self, _event=None):
        sel = self.listbox.curselection()
        self.update_print_label()
        if not sel:
            return  # selezione svuotata: l'anteprima resta sull'ultima foto vista
        anchor = self.listbox.index("anchor")  # l'ultima foto su cui hai cliccato
        idx = anchor if anchor in sel else sel[0]
        if idx != self.preview_index:
            self.set_preview_index(idx)
            self.schedule_preview()

    def setup_dnd(self):
        """Trascinamento di file e cartelle sulla finestra (richiede tkinterdnd2)."""
        if not self.dnd:
            return
        for w in (self.root, self.listbox, self.canvas):
            w.drop_target_register(DND_FILES)
            w.dnd_bind("<<Drop>>", self.on_drop)

    def on_drop(self, event):
        files = self.root.tk.splitlist(event.data)  # gestisce i percorsi con spazi tra parentesi graffe
        if not self.add_paths(files):
            self.set_status("drop_none")
        return event.action

    def add_photos(self):
        files = filedialog.askopenfilenames(
            title=self.t("choose_photos"),
            filetypes=[(self.t("ft_images"), IMAGE_EXT), (self.t("ft_all"), "*.*")],
            initialdir=self.settings.get("folder") or None)
        if files:
            self.settings["folder"] = os.path.dirname(files[0])
            self.add_paths(files, only_images=False)

    def add_paths(self, files, only_images: bool = True) -> int:
        """Aggiunge file (o le immagini contenute in una cartella) all'elenco. Ritorna quante ne ha aggiunte."""
        found = []
        for f in files:
            if os.path.isdir(f):
                found += [os.path.join(f, n) for n in sorted(os.listdir(f)) if n.lower().endswith(IMAGE_EXTS)]
            elif os.path.isfile(f) and (not only_images or f.lower().endswith(IMAGE_EXTS)):
                found.append(f)
        added = 0
        for f in found:
            if f not in self.paths:
                self.paths.append(f)
                self.listbox.insert("end", self.label_for(f))
                added += 1
        if self.preview_index is None and self.paths:
            self.listbox.selection_set(0)
            self.set_preview_index(0)
        self.update_print_label()
        if added:
            self.schedule_preview()
        return added

    def remove_photos(self):
        sel = list(self.listbox.curselection())
        if not sel:
            return
        for i in reversed(sel):
            self.listbox.delete(i)
            self.rot.pop(self.paths[i], None)
            del self.paths[i]
        if not self.paths:
            self.preview_index = None
        elif self.preview_index is not None:
            if self.preview_index in sel:
                new = min(sel[0], len(self.paths) - 1)
            else:
                new = self.preview_index - sum(1 for i in sel if i < self.preview_index)
            self.listbox.selection_clear(0, "end")
            self.listbox.selection_set(new)
            self.set_preview_index(new)
        self.update_print_label()
        self.schedule_preview()

    def clear_photos(self):
        self.listbox.delete(0, "end")
        self.paths.clear()
        self.rot.clear()
        self.preview_index = None
        self.update_print_label()
        self.schedule_preview()

    def rotate(self, delta: int):
        """Ruota le foto selezionate (o quella in anteprima se non c'e' selezione)."""
        targets = list(self.listbox.curselection())
        if not targets and self.preview_index is not None:
            targets = [self.preview_index]
        if not targets:
            return
        for i in targets:
            path = self.paths[i]
            self.rot[path] = (self.rot.get(path, 0) + delta) % 360
            self.refresh_label(i)
        if self.preview_index is not None:
            self.listbox.selection_anchor(self.preview_index)
        if self.preview_index in targets:
            self.schedule_preview()

    # ---------- anteprima ----------
    def schedule_preview(self):
        if self.preview_after is not None:
            self.root.after_cancel(self.preview_after)
        self.preview_after = self.root.after(PREVIEW_DELAY_MS, self.start_preview)

    def current_path(self):
        if self.preview_index is not None and 0 <= self.preview_index < len(self.paths):
            return self.paths[self.preview_index]
        return self.paths[0] if self.paths else None

    def start_preview(self):
        self.preview_after = None
        path = self.current_path()
        self.preview_seq += 1
        seq = self.preview_seq
        if path is None:
            self.canvas.delete("all")
            self.preview_photo = None
            self.preview_name = None
            self.render_cache = None
            self.update_preview_title()
            return
        if self.worker is None and not self.querying:
            self.set_status("calc_preview")
        method, gamma = self.method_id, round(self.gamma_var.get(), 2)
        thr, rot, width = int(round(self.thr_var.get())), self.rot.get(path, 0), self.get_width()
        adjust = self.get_adjust()

        def job():
            try:
                gray, burn = render(path, method, gamma, thr, rot, width, adjust)
                gray = np.clip(gray, 0, 255).astype(np.uint8)
                self.q.put(("preview", seq, os.path.basename(path), gray, burn))
            except Exception as e:  # file non leggibile, ecc.
                self.q.put(("preview_error", seq, f"{os.path.basename(path)}: {e}"))

        threading.Thread(target=job, daemon=True).start()

    def show_preview(self, name: str, gray: np.ndarray, burn: np.ndarray, quiet: bool = False):
        """Mostra il risultato; con "Affianca originale" mette a sinistra l'immagine di partenza."""
        self.render_cache = (name, gray, burn)
        self.preview_name = name
        burn_img = Image.fromarray(np.where(burn, 0, 255).astype(np.uint8), "L")
        h, w = burn.shape
        self.canvas.delete("all")
        if self.compare_var.get():
            gap, top = 8, 20
            avail = self.canvas.winfo_width() - 8
            scale = (avail - gap) / (2 * w) if 300 < avail < 2 * w + gap else 1.0
            left, right = Image.fromarray(gray, "L"), burn_img
            if scale < 1.0:  # stessa riduzione (media per area) sulle due meta'
                size = (max(1, round(w * scale)), max(1, round(h * scale)))
                left, right = left.resize(size, Image.Resampling.BOX), right.resize(size, Image.Resampling.BOX)
            cw, ch = left.size
            sheet = Image.new("L", (2 * cw + gap, ch), 128)
            sheet.paste(left, (0, 0))
            sheet.paste(right, (cw + gap, 0))
            self.preview_photo = ImageTk.PhotoImage(sheet)
            self.canvas.create_image(2, top, anchor="nw", image=self.preview_photo)
            note = f" ({self.t('reduced', p=round(scale * 100))})" if scale < 1.0 else ""
            self.canvas.create_text(4, 3, anchor="nw", text=self.t("orig") + note)
            self.canvas.create_text(cw + gap + 4, 3, anchor="nw", text=self.t("result") + note)
            region = (0, 0, sheet.width + 4, ch + top + 4)
        else:
            self.preview_photo = ImageTk.PhotoImage(burn_img)
            self.canvas.create_image(2, 2, anchor="nw", image=self.preview_photo)
            region = (0, 0, w + 4, h + 4)
        self.canvas.configure(scrollregion=region)
        if not quiet:
            self.canvas.yview_moveto(0)
            self.canvas.xview_moveto(0)
        self.update_preview_title()
        if not quiet and self.worker is None and not self.querying:
            self.set_status("preview_info", w=w, h=h, cm=h / DOTS_PER_CM)

    def on_canvas_resize(self, event):
        """Con il confronto attivo, riadatta le due meta' quando cambia la larghezza della finestra."""
        if not (self.compare_var.get() and self.render_cache) or abs(event.width - self.last_canvas_w) < 12:
            return
        self.last_canvas_w = event.width
        if self.resize_after is not None:
            self.root.after_cancel(self.resize_after)
        self.resize_after = self.root.after(250, lambda: self.show_preview(*self.render_cache, quiet=True))

    def on_compare(self):
        if self.render_cache:
            self.show_preview(*self.render_cache)
        else:
            self.schedule_preview()

    # ---------- stampa ----------
    def conc_value(self):
        return int(self.conc_id) if self.conc_id in ("0", "1", "2") else None

    def set_busy(self, busy: bool):
        state = "disabled" if busy else "normal"
        for b in (self.print_btn, self.ruler_btn, self.status_btn):
            b.configure(state=state)
        self.cancel_btn.configure(state="normal" if busy else "disabled")

    def start_jobs(self, jobs, done_key=None):
        """jobs: elenco di (nome, funzione che ritorna la matrice di punti da stampare)."""
        com = self.selected_port()
        if not com:
            messagebox.showinfo(APP_NAME, self.t("need_com"))
            return
        self.cancel.clear()
        self.progress["value"] = 0
        self.set_busy(True)
        self.worker = threading.Thread(target=self.print_worker, args=(jobs, com, self.conc_value(), done_key),
                                       daemon=True)
        self.worker.start()

    def start_print(self):
        if self.worker is not None or self.querying:
            return
        if not self.paths:
            messagebox.showinfo(APP_NAME, self.t("need_photo"))
            return
        sel = self.listbox.curselection()
        paths = [self.paths[i] for i in sel] if sel else list(self.paths)
        method, gamma = self.method_id, round(self.gamma_var.get(), 2)
        thr, width, rot, adjust = int(round(self.thr_var.get())), self.get_width(), dict(self.rot), self.get_adjust()
        jobs = [(os.path.basename(p),
                 lambda p=p: make_burn(p, method, gamma, thr, rot.get(p, 0), width, adjust)) for p in paths]
        self.start_jobs(jobs)

    def start_ruler(self):
        if self.worker is None and not self.querying:
            self.start_jobs([(self.t("ruler_name"), pd.ruler_burn)], done_key="done_ruler")

    def check_printer(self):
        """Legge nome, firmware e batteria dalla porta selezionata (comandi di sola lettura)."""
        if self.worker is not None or self.querying:
            return
        port = self.selected_port()
        if not port:
            messagebox.showinfo(APP_NAME, self.t("need_com"))
            return
        self.querying = True
        self.set_busy(True)
        self.cancel_btn.configure(state="disabled")
        self.set_status("checking", port=port)

        def job():
            try:
                self.q.put(("printer", port, pd.query_printer(port, BAUD)))
            except Exception as e:
                self.q.put(("printer_fail", port, str(e)))

        threading.Thread(target=job, daemon=True).start()

    def show_printer_result(self):
        r = self.printer_result
        if r is None:
            self.status_info.set("")
        elif r[0] == "fail":
            self.status_info.set(self.t("st_fail", port=r[1], msg=r[2]))
        elif r[0] == "none":
            self.status_info.set(self.t("st_none", port=r[1]))
        else:
            info, lines = r[1], []
            if info["name"]:
                lines.append(f"{self.t('st_name')}: {info['name']}")
            if info["firmware"]:
                lines.append(f"{self.t('st_fw')}: {info['firmware']}")
            if info["dpi"]:
                lines.append(f"{self.t('st_dpi')}: {info['dpi']} dpi")
            if info["battery"] is not None:
                low = f" ({self.t('st_low')})" if info["battery"] <= LOW_BATTERY else ""
                lines.append(f"{self.t('st_batt')}: {info['battery']}%{low}")
            self.status_info.set("\n".join(lines))

    def cancel_print(self):
        self.cancel.set()
        self.set_status("cancelling")

    def print_worker(self, jobs, com, conc, done_key=None):
        n = len(jobs)
        put = self.q.put
        try:
            for i, (name, make) in enumerate(jobs):
                if self.cancel.is_set():
                    break
                put(("status", "preparing", dict(i=i + 1, n=n, name=name)))
                burn = make()
                burn = np.vstack([burn, np.zeros((TAIL_FEED_ROWS, burn.shape[1]), dtype=bool)])
                body, tail = pd.driver_stream(burn, conc)
                if self.cancel.is_set():
                    break
                put(("status", "sending", dict(i=i + 1, n=n, name=name, com=com)))
                last = [-1.0]

                def progress(done, total, i=i):
                    frac = (i + done / total) / n
                    if frac - last[0] >= 0.005:
                        last[0] = frac
                        put(("progress", frac))

                if pd.send_serial(com, [body, tail], SEND_DELAY, BAUD, progress, self.cancel):
                    break
                if i < n - 1:  # lascia finire la stampa prima del reset della foto successiva
                    end = time.time() + PAUSE_BETWEEN_JOBS
                    while time.time() < end and not self.cancel.is_set():
                        put(("status", "waiting", dict(i=i + 1, n=n, s=f"{end - time.time():.0f}")))
                        time.sleep(0.2)
            if self.cancel.is_set():
                put(("done", "cancelled", {}))
            else:
                put(("done", done_key or ("done_1" if n == 1 else "done"), dict(n=n)))
        except Exception as e:
            put(("error", str(e)))

    # ---------- coda messaggi dai thread ----------
    def poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "preview" and msg[1] == self.preview_seq:
                    self.show_preview(msg[2], msg[3], msg[4])
                elif kind == "preview_error" and msg[1] == self.preview_seq:
                    self.set_status("preview_failed", msg=msg[2])
                elif kind == "status":
                    self.set_status(msg[1], **msg[2])
                elif kind == "progress":
                    self.progress["value"] = msg[1] * 100
                elif kind == "printer":
                    info = msg[2]
                    self.querying = False
                    self.set_busy(False)
                    if any(info[k] is not None for k in ("name", "firmware", "battery")):
                        self.printer_result = ("ok", info, msg[1])
                        self.set_status("status_done")
                    else:
                        self.printer_result = ("none", msg[1])
                        self.set_status("status_nodata")
                    self.show_printer_result()
                elif kind == "printer_fail":
                    self.querying = False
                    self.set_busy(False)
                    self.printer_result = ("fail", msg[1], msg[2])
                    self.set_status("status_nodata")
                    self.show_printer_result()
                elif kind == "done":
                    self.worker = None
                    self.set_busy(False)
                    if msg[1] != "cancelled":
                        self.progress["value"] = 100
                    self.set_status(msg[1], **msg[2])
                elif kind == "error":
                    self.worker = None
                    self.set_busy(False)
                    self.set_status("print_error")
                    messagebox.showerror(APP_NAME, self.t("error_hint", msg=msg[1]))
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def on_close(self):
        if self.worker is not None:
            if not messagebox.askyesno(APP_NAME, self.t("confirm_quit")):
                return
            self.cancel.set()
            self.worker.join(timeout=5)
        self.settings.update(com=self.selected_port(), dither=self.method_id,
                             gamma=round(self.gamma_var.get(), 2), conc=self.conc_id,
                             threshold=int(round(self.thr_var.get())), lang=self.lang, width=self.get_width(),
                             sharpen=int(round(self.sharp_var.get())), clarity=round(self.clar_var.get(), 2),
                             autocontrast=round(self.auto_var.get(), 1), compare=bool(self.compare_var.get()))
        save_settings(self.settings)
        self.root.destroy()


def main():
    try:  # su Windows evita l'interfaccia sfocata sugli schermi con scala diversa da 100%
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (ImportError, AttributeError, OSError):
        pass
    root = None
    if TkinterDnD is not None:
        try:
            root = TkinterDnD.Tk()
        except Exception:  # libreria tkdnd non caricabile: l'app funziona senza trascinamento
            root = None
    if root is None:
        root = tk.Tk()
    ttk.Style().theme_use("vista" if "vista" in ttk.Style().theme_names() else "clam")
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
