#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""
Graba solo las calls de Teams (el audio de la call + el microfono), sin OBS.

    pythonw grabador_calls.py

Queda un icono al lado del reloj: gris esperando, rojo grabando, ambar con la
deteccion pausada. Cuando la call termina deja un .mp3 en Videos\Calls.

COMO SABE QUE HAY UNA CALL: mira las sesiones de audio de Windows; si un
proceso de Teams esta capturando de un microfono, hay call. Eso ademas dice que
dispositivos usa la call, que no siempre son los predeterminados (por ejemplo
con NVIDIA Broadcast o un microfono virtual en el medio). De respaldo mira el
registro (CapabilityAccessManager\ConsentStore\microphone). El mute de Teams
NO cierra el microfono, asi que una call en silencio se sigue grabando. Lo que
no detecta: entrar con el microfono desactivado desde antes -> "Grabar ahora".

POR QUE DOS WAV Y NO UNO: la call sale por WASAPI loopback (lo que suena en
los auriculares) y la voz propia por el microfono; son dos dispositivos con su
propio reloj. Se graban por separado y ffmpeg los mezcla al final. Las pistas
sueltas quedan en Calls\pistas\ en .flac, por si hace falta saber quien dijo
que.

EL LOOPBACK NO ENTREGA NADA MIENTRAS NO SUENA NADA: si no se rellenara, cada
silencio de la call adelantaria la pista contra la del microfono. Por eso cada
pista compara lo que escribio contra el reloj y completa con ceros.

TRANSCRIBIR (opcional): si la variable de entorno WHISPERX340_DIR apunta a un
proyecto de WhisperX340, el menu ofrece "Transcribir la ultima".
"""
import ctypes
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import wave
import winreg
from datetime import datetime
from pathlib import Path

_whisper = os.environ.get("WHISPERX340_DIR")
WHISPER = Path(_whisper) if _whisper else None
LOG = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "GrabadorCalls" / "grabador_calls.log"
CARPETA = Path.home() / "Videos" / "Calls"
EN_CURSO = CARPETA / ".en-curso"
PISTAS = CARPETA / "pistas"
FIRMA = "By BrianX340"

# Dispositivos: None = los que usa Teams (o los predeterminados de Windows si
# no se pueden leer). Para forzar otros, poner aca un pedazo del nombre.
MICROFONO = None
SALIDA = None

CADA = 2.0           # segundos entre lecturas
ARRANCAR_TRAS = 3.0  # el microfono tiene que seguir abierto este tiempo
CORTAR_TRAS = 15.0   # y cerrado este otro, para no partir una call en dos
HUECO = 0.10         # un atraso mayor a esto en una pista se rellena con silencio

MIC_REG = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
TEAMS_NUEVO = "MSTeams_8wekyb3d8bbwe"

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    import pyaudiowpatch as pa
    from PIL import Image, ImageDraw
    from pystray import Icon, Menu, MenuItem
except ImportError as e:
    sys.exit(f"Falta una dependencia ({e.name}).\n"
             f"Instalar con:  python -m pip install -r requirements.txt")

LOG.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(filename=LOG, encoding="utf-8", level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("grabador")


# ── deteccion ────────────────────────────────────────────────────────────────

def claves_de_teams():
    """El Teams nuevo es una app empaquetada; el clasico aparece en NonPackaged
    con la ruta del exe (C:#Users#...#Teams.exe)."""
    claves = [MIC_REG + "\\" + TEAMS_NUEVO]
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, MIC_REG + r"\NonPackaged") as k:
            i = 0
            while True:
                try:
                    nombre = winreg.EnumKey(k, i)
                except OSError:
                    break
                if "teams" in nombre.lower():
                    claves.append(MIC_REG + r"\NonPackaged" + "\\" + nombre)
                i += 1
    except OSError:
        pass
    return claves


def sesiones_de_teams():
    """(microfono, salida) que Teams tiene abiertos ahora, o None si no captura.

    Es la deteccion principal: con un microfono virtual en el medio (NVIDIA
    Broadcast, por ejemplo) el registro no se entera de Teams, y ademas esto
    dice QUE dispositivos usa la call, que no siempre son los predeterminados."""
    import comtypes
    import psutil
    from pycaw.pycaw import AudioUtilities, IAudioSessionControl2, IAudioSessionManager2, IMMDeviceEnumerator
    from pycaw.constants import CLSID_MMDeviceEnumerator

    enum = comtypes.CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator,
                                     comtypes.CLSCTX_INPROC_SERVER)
    encontrados = {}
    for flujo in (0, 1):  # eRender, eCapture
        col = enum.EnumAudioEndpoints(flujo, 1)  # DEVICE_STATE_ACTIVE
        for i in range(col.GetCount()):
            disp = col.Item(i)
            # QueryInterface y no cast(): cast no suma referencia y comtypes la
            # libera igual al recolectar -> a las ~100 vueltas, acceso invalido
            mgr = disp.Activate(IAudioSessionManager2._iid_, comtypes.CLSCTX_ALL, None
                                ).QueryInterface(IAudioSessionManager2)
            sesiones = mgr.GetSessionEnumerator()
            for j in range(sesiones.GetCount()):
                ctl = sesiones.GetSession(j).QueryInterface(IAudioSessionControl2)
                if ctl.GetState() != 1:  # AudioSessionStateActive
                    continue
                try:
                    nombre = psutil.Process(ctl.GetProcessId()).name().lower()
                except Exception:
                    continue
                if "teams" in nombre:
                    encontrados.setdefault(flujo, AudioUtilities.CreateDevice(disp).FriendlyName)
    if 1 not in encontrados:
        return None
    return encontrados[1], encontrados.get(0)


def teams_usa_el_microfono():
    for clave in claves_de_teams():
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, clave) as k:
                inicio, _ = winreg.QueryValueEx(k, "LastUsedTimeStart")
                fin, _ = winreg.QueryValueEx(k, "LastUsedTimeStop")
        except OSError:
            continue
        if inicio and fin == 0:
            return True
    return False


# ── grabacion ────────────────────────────────────────────────────────────────

class Pista:
    """Un dispositivo -> un WAV de 16 bits, alineado contra el reloj comun t0."""

    def __init__(self, audio, disp, ruta, t0):
        self.nombre = disp["name"]
        self.tasa = int(disp["defaultSampleRate"])
        self.canales = max(1, min(2, int(disp["maxInputChannels"])))
        self.bytes_frame = 2 * self.canales
        self.t0 = t0
        self.escritos = 0
        self.lock = threading.Lock()
        self.wav = wave.open(str(ruta), "wb")
        self.wav.setnchannels(self.canales)
        self.wav.setsampwidth(2)
        self.wav.setframerate(self.tasa)
        self.stream = audio.open(format=pa.paInt16, channels=self.canales, rate=self.tasa,
                                 input=True, input_device_index=disp["index"],
                                 frames_per_buffer=1024, stream_callback=self._cb)

    def _rellenar_hasta(self, frame):
        falta = frame - self.escritos
        if falta > self.tasa * HUECO:
            self.wav.writeframes(b"\x00" * (falta * self.bytes_frame))
            self.escritos += falta

    def _cb(self, datos, n, _info, _estado):
        with self.lock:
            if self.wav is None:
                return (None, pa.paComplete)
            # el buffer que llega termina "ahora": su primer frame es ahora - n
            self._rellenar_hasta(int((time.monotonic() - self.t0) * self.tasa) - n)
            self.wav.writeframes(datos)
            self.escritos += n
        return (None, pa.paContinue)

    def cerrar(self):
        try:
            self.stream.stop_stream()
            self.stream.close()
        except Exception:
            log.exception("cerrando %s", self.nombre)
        with self.lock:
            # que las dos pistas terminen a la misma altura
            self._rellenar_hasta(int((time.monotonic() - self.t0) * self.tasa))
            self.wav.close()
            self.wav = None


def buscar(audio, nombre, loopback):
    """El dispositivo pedido por nombre, o el predeterminado de WASAPI."""
    if nombre:
        for d in (audio.get_loopback_device_info_generator() if loopback
                  else (audio.get_device_info_by_index(i) for i in range(audio.get_device_count()))):
            if nombre.lower() in d["name"].lower() and d["maxInputChannels"] > 0:
                if loopback or d["hostApi"] == audio.get_host_api_info_by_type(pa.paWASAPI)["index"]:
                    return d
        log.warning("no encontre '%s', uso el predeterminado", nombre)
    if loopback:
        return audio.get_default_wasapi_loopback()
    wasapi = audio.get_host_api_info_by_type(pa.paWASAPI)
    return audio.get_device_info_by_index(wasapi["defaultInputDevice"])


class Grabacion:
    def __init__(self, mic=None, salida=None):
        self.base = datetime.now().strftime("%Y-%m-%d %H-%M-%S")
        self.inicio = time.monotonic()
        EN_CURSO.mkdir(parents=True, exist_ok=True)
        self.audio = pa.PyAudio()
        self.wav_mic = EN_CURSO / f"{self.base} mic.wav"
        self.wav_call = EN_CURSO / f"{self.base} call.wav"
        t0 = time.monotonic()
        self.pistas = []
        try:
            self.pistas.append(Pista(self.audio, buscar(self.audio, MICROFONO or mic, False), self.wav_mic, t0))
            self.pistas.append(Pista(self.audio, buscar(self.audio, SALIDA or salida, True), self.wav_call, t0))
        except Exception:
            for p in self.pistas:
                p.cerrar()
            self.audio.terminate()
            raise
        log.info("grabando %s | mic=%s | call=%s", self.base, *[p.nombre for p in self.pistas])

    def duracion(self):
        s = int(time.monotonic() - self.inicio)
        return f"{s // 60}:{s % 60:02d}"

    def detener(self):
        for p in self.pistas:
            p.cerrar()
        self.audio.terminate()
        log.info("detenida %s (%s)", self.base, self.duracion())

    def finalizar(self):
        """Mezcla las dos pistas en un mp3 mono y guarda las sueltas en flac.
        Si ffmpeg falla, los WAV quedan en .en-curso para no perder nada."""
        mp3 = CARPETA / f"{self.base}.mp3"
        PISTAS.mkdir(parents=True, exist_ok=True)
        mezcla = ("[0:a]aformat=channel_layouts=mono[a];[1:a]aformat=channel_layouts=mono[b];"
                  "[a][b]amix=inputs=2:duration=longest:normalize=0[m]")
        cmds = [
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(self.wav_mic), "-i", str(self.wav_call),
             "-filter_complex", mezcla, "-map", "[m]", "-c:a", "libmp3lame", "-b:a", "96k", str(mp3)],
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(self.wav_mic),
             str(PISTAS / f"{self.base} mic.flac")],
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(self.wav_call),
             str(PISTAS / f"{self.base} call.flac")],
        ]
        for cmd in cmds:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            if r.returncode != 0:
                log.error("ffmpeg fallo (%s): %s", cmd[-1], r.stderr.strip())
                return None
        self.wav_mic.unlink(missing_ok=True)
        self.wav_call.unlink(missing_ok=True)
        log.info("lista %s", mp3)
        return mp3


# ── icono ────────────────────────────────────────────────────────────────────

def dibujar_icono(color, lado=64):
    """Un microfono sobre un cuadrado oscuro y el punto de estado abajo a la
    derecha. Se dibuja a 256 y se achica: a 16x16 sin eso sale serruchado."""
    S = 256
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    # fondo: degrade vertical en un cuadrado redondeado
    fondo = Image.new("RGBA", (S, S))
    fd = ImageDraw.Draw(fondo)
    for y in range(S):
        t = y / S
        fd.line((0, y, S, y), fill=(int(38 - 18 * t), int(42 - 20 * t), int(58 - 26 * t), 255))
    mascara = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mascara).rounded_rectangle((8, 8, S - 8, S - 8), radius=56, fill=255)
    img.paste(fondo, (0, 0), mascara)
    d = ImageDraw.Draw(img)
    blanco = (238, 240, 245, 255)
    # capsula, arco del soporte, pie y base
    d.rounded_rectangle((96, 44, 160, 150), radius=32, fill=blanco)
    for y in (78, 98, 118):  # rejilla
        d.line((112, y, 144, y), fill=(160, 166, 182, 255), width=6)
    d.arc((72, 86, 184, 184), start=0, end=180, fill=blanco, width=12)
    d.line((128, 184, 128, 210), fill=blanco, width=12)
    d.rounded_rectangle((92, 204, 164, 216), radius=6, fill=blanco)
    # punto de estado con borde del color del fondo, para que se despegue
    d.ellipse((150, 150, 246, 246), fill=(20, 22, 32, 255))
    d.ellipse((162, 162, 234, 234), fill=color + (255,))
    return img.resize((lado, lado), Image.LANCZOS)


GRIS, ROJO, AMBAR = (120, 125, 135), (230, 60, 60), (255, 176, 32)


class App:
    def __init__(self):
        self.lock = threading.Lock()
        self.grabacion = None
        self.manual = False
        self.detectar = True
        self.ultima = None
        self.vivo = True
        self.icon = Icon("grabador_calls", dibujar_icono(GRIS), f"Grabador de calls\n{FIRMA}", self._menu())

    def _menu(self):
        return Menu(
            MenuItem(lambda _: self.estado(), None, enabled=False),
            Menu.SEPARATOR,
            MenuItem("Detectar calls de Teams", self.alternar_deteccion, checked=lambda _: self.detectar),
            MenuItem(lambda _: "Detener grabación" if self.grabacion else "Grabar ahora",
                     self.alternar_manual),
            Menu.SEPARATOR,
            MenuItem("Abrir carpeta de grabaciones", self.abrir_carpeta),
            MenuItem("Transcribir la última", self.transcribir, enabled=lambda _: self.ultima_mp3() is not None),
            Menu.SEPARATOR,
            MenuItem("Salir", self.salir),
            Menu.SEPARATOR,
            MenuItem(f"Grabador de calls · {FIRMA}", None, enabled=False),
        )

    def estado(self):
        if self.grabacion:
            return f"● Grabando {self.grabacion.duracion()}" + (" (manual)" if self.manual else "")
        return "Esperando una call" if self.detectar else "Detección pausada"

    def refrescar(self):
        color = ROJO if self.grabacion else (GRIS if self.detectar else AMBAR)
        self.icon.icon = dibujar_icono(color)
        self.icon.title = f"Grabador de calls — {self.estado()}\n{FIRMA}"
        self.icon.update_menu()

    # ── arrancar / cortar ──

    def arrancar(self, manual, mic=None, salida=None):
        with self.lock:
            if self.grabacion:
                return
            try:
                self.grabacion = Grabacion(mic, salida)
            except Exception as e:
                log.exception("no pude arrancar")
                self.icon.notify(f"No pude arrancar a grabar: {e}", "Grabador de calls")
                return
            self.manual = manual
        self.refrescar()
        self.icon.notify("Grabando la call (audio + micrófono)", "Grabador de calls")

    def cortar(self):
        with self.lock:
            g, self.grabacion, self.manual = self.grabacion, None, False
        if not g:
            return
        g.detener()
        self.refrescar()
        threading.Thread(target=self._finalizar, args=(g,), daemon=False).start()

    def _finalizar(self, g):
        mp3 = g.finalizar()
        if mp3:
            self.ultima = mp3
            self.icon.notify(f"Grabación lista: {mp3.name} ({g.duracion()})", "Grabador de calls")
        else:
            self.icon.notify(f"Falló la mezcla; los WAV quedaron en {EN_CURSO}", "Grabador de calls")
        self.refrescar()

    # ── vigilancia ──

    def vigilar(self):
        import comtypes
        comtypes.CoInitialize()  # COM es por hilo
        en_uso_desde = libre_desde = None
        while self.vivo:
            try:
                if self.detectar:
                    ahora = time.monotonic()
                    try:
                        dispositivos = sesiones_de_teams()
                    except Exception:
                        log.exception("leyendo sesiones de audio")
                        dispositivos = None
                    if dispositivos or teams_usa_el_microfono():
                        libre_desde = None
                        en_uso_desde = en_uso_desde or ahora
                        if not self.grabacion and ahora - en_uso_desde >= ARRANCAR_TRAS:
                            self.arrancar(False, *(dispositivos or ()))
                    else:
                        en_uso_desde = None
                        libre_desde = libre_desde or ahora
                        if self.grabacion and not self.manual and ahora - libre_desde >= CORTAR_TRAS:
                            self.cortar()
                if self.grabacion:
                    self.refrescar()  # el contador del menu
            except Exception:
                log.exception("vigilando")
            time.sleep(CADA)

    # ── menu ──

    def alternar_deteccion(self, *_):
        self.detectar = not self.detectar
        self.refrescar()

    def alternar_manual(self, *_):
        if self.grabacion:
            self.cortar()
        else:
            self.arrancar(manual=True)

    def abrir_carpeta(self, *_):
        CARPETA.mkdir(parents=True, exist_ok=True)
        os.startfile(CARPETA)

    def ultima_mp3(self):
        if self.ultima and self.ultima.exists():
            return self.ultima
        mp3s = sorted(CARPETA.glob("*.mp3"), key=lambda p: p.stat().st_mtime) if CARPETA.exists() else []
        return mp3s[-1] if mp3s else None

    def transcribir(self, *_):
        mp3 = self.ultima_mp3()
        uv = shutil.which("uv")
        if not mp3 or not uv or not WHISPER or not WHISPER.exists():
            self.icon.notify("Falta el mp3, uv o WHISPERX340_DIR", "Grabador de calls")
            return
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        # en consola propia: tarda unos minutos y conviene ver el avance
        subprocess.Popen(["cmd", "/k", uv, "run", "--no-sync", "--project", str(WHISPER),
                          "whisperx340", str(mp3), "--hablantes"],
                         env=env, creationflags=subprocess.CREATE_NEW_CONSOLE)

    def salir(self, *_):
        self.vivo = False
        self.cortar()  # el hilo de ffmpeg no es daemon: termina de mezclar antes de morir
        self.icon.stop()

    def rescatar(self):
        """Si el proceso murio grabando, los WAV quedaron en .en-curso: se
        mezclan al arrancar, como si la grabacion hubiera terminado bien."""
        for mic in sorted(EN_CURSO.glob("* mic.wav")) if EN_CURSO.exists() else []:
            base = mic.name[:-len(" mic.wav")]
            if not (EN_CURSO / f"{base} call.wav").exists():
                continue
            g = Grabacion.__new__(Grabacion)
            g.base, g.wav_mic, g.wav_call = base, mic, EN_CURSO / f"{base} call.wav"
            log.info("rescatando %s", base)
            if mp3 := g.finalizar():
                self.ultima = mp3

    def correr(self):
        threading.excepthook = lambda a: log.error("hilo %s", a.thread, exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
        threading.Thread(target=self.rescatar, daemon=False).start()
        threading.Thread(target=self.vigilar, daemon=True).start()
        self.icon.run(setup=lambda icon: setattr(icon, "visible", True))


def una_sola_instancia():
    ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\grabador_calls_x340")
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


if __name__ == "__main__":
    if not una_sola_instancia():
        sys.exit("Ya hay un grabador corriendo (mirá la bandeja).")
    log.info("arranca")
    App().correr()
