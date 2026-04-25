"""
KLEANVET - Sistema de Auditoria de Etiquetas
Backend FastAPI con OCR via OpenAI y validacion contra Airtable.
"""

import os
import io
import json
import asyncio
import base64
from datetime import datetime
from typing import Optional
from uuid import uuid4

import httpx
from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi import Request
from fastapi.middleware.cors import CORSMiddleware
from pyairtable import Api as AirtableApi
from PIL import Image
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# CONFIGURACION
# ============================================================

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
AIRTABLE_TABLE_PRESCRIPCIONES = os.getenv("AIRTABLE_TABLE_PRESCRIPCIONES", "Prescripciones")
AIRTABLE_TABLE_AUDITORIAS = os.getenv("AIRTABLE_TABLE_AUDITORIAS", "Auditorias")
N8N_REPORT_WEBHOOK_URL = os.getenv("N8N_REPORT_WEBHOOK_URL")
MAX_CONCURRENT_OCR = int(os.getenv("MAX_CONCURRENT_OCR", "5"))

# Validar credenciales al arrancar
if not all([OPENAI_API_KEY, AIRTABLE_API_KEY, AIRTABLE_BASE_ID]):
    print("ADVERTENCIA: Faltan variables de entorno. Revisa el archivo .env")

# ============================================================
# APP FASTAPI
# ============================================================

app = FastAPI(title="Kleanvet Auditoria")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Estado en memoria de cortes en proceso (simple, sin Redis)
cortes_en_proceso = {}

# ============================================================
# PROMPTS DE OCR
# ============================================================

PROMPT_CAJA = """Eres un motor de extraccion OCR para etiquetas farmaceuticas veterinarias.

La imagen contiene una o varias etiquetas de CAJA. Extrae cada etiqueta como un objeto en un array JSON.

REGLAS CRITICAS:
- "Martha Lopez" SIEMPRE va en preparado_por, NUNCA en nombre_acudiente
- Si no hay nombre acudiente legible, devuelve null
- Lectura vertical: el valor esta debajo del titulo
- No mezcles datos entre etiquetas vecinas

CAMPOS por etiqueta:
1. numero_lote (bajo "N° Lote")
2. nombre_paciente
3. nombre_acudiente (NO Martha Lopez)
4. documento_acudiente
5. ubicacion
6. preparado_por (aqui SI Martha Lopez)
7. fecha_hora_preparacion
8. fecha_limite_uso
9. dosis
10. via_administracion
11. producto: "CBD X%" donde X es cualquier numero (ej: CBD 1%, CBD 3%). Null si no esta.
12. codigo_formato: codigo arriba derecha tipo CP-FM-FO-218

Responde SOLO con array JSON valido, sin markdown:
[{...}, {...}]"""

PROMPT_FRASCO = PROMPT_CAJA.replace("CAJA", "FRASCO").replace("CP-FM-FO-218", "CP-FM-FO-217")

PROMPT_PRESCRIPCION = """Eres un motor OCR para recetas medicas veterinarias.

La imagen contiene UNA prescripcion. Extrae los datos.

CAMPO numero_prescripcion - CRITICO:
SOLO extraelo si esta precedido EXPLICITAMENTE por "Receta N°", "Rx N°", "Prescripcion N°", "Folio N°".
NO extraigas como numero_prescripcion: cedulas, codigos de barras, telefonos, fechas, lotes.
Si tienes duda, devuelve null. Prefiere null sobre inventar.

CAMPOS:
- fecha_prescripcion (DD/MM/AAAA)
- nombre_paciente
- nombre_acudiente
- documento_acudiente
- medicamento_formulado
- via_administracion
- dosis_instrucciones
- numero_prescripcion (ver reglas arriba)

Responde SOLO con objeto JSON plano, sin markdown."""

PROMPT_ENVIO = """Eres un motor OCR para guias de envio.

La imagen contiene una o varias etiquetas de envio. Extrae datos del DESTINATARIO (no del remitente).

CAMPOS por etiqueta:
- nombre_destinatario
- direccion_entrega
- ciudad_destino
- telefono_contacto
- numero_guia_referencia
- documento_destinatario
- nombre_mascota (si aparece)

Responde SOLO con array JSON, sin markdown."""

# ============================================================
# HELPERS
# ============================================================

def normalize_text(text: str) -> str:
    """Normaliza texto para comparacion fuzzy."""
    if not text:
        return ""
    import unicodedata
    nfkd = unicodedata.normalize("NFD", str(text).lower())
    only_ascii = "".join(c for c in nfkd if unicodedata.category(c) != "Mn")
    return "".join(c for c in only_ascii if c.isalnum())


def similarity(a: str, b: str) -> float:
    """Score de similitud simple entre dos strings."""
    if not a or not b:
        return 0.0
    na, nb = normalize_text(a), normalize_text(b)
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return 0.9
    longer, shorter = (na, nb) if len(na) > len(nb) else (nb, na)
    if not longer:
        return 0.0
    matches = sum(1 for ch in shorter if ch in longer)
    return matches / len(longer)


def classify_filename(filename: str) -> Optional[str]:
    """Clasifica el archivo segun su nombre."""
    name_upper = filename.upper()
    if "CAJA" in name_upper:
        return "caja"
    if "FRASCO" in name_upper:
        return "frasco"
    if "PRESCRIPCION" in name_upper or "RECETA" in name_upper:
        return "prescripcion"
    if "ENVIO" in name_upper or "ENVÍO" in name_upper or "GUIA" in name_upper:
        return "envio"
    return None


def get_prompt_for_type(tipo: str) -> str:
    return {
        "caja": PROMPT_CAJA,
        "frasco": PROMPT_FRASCO,
        "prescripcion": PROMPT_PRESCRIPCION,
        "envio": PROMPT_ENVIO,
    }.get(tipo, PROMPT_CAJA)


async def image_to_base64(file_bytes: bytes, filename: str) -> str:
    """Convierte cualquier formato a PNG base64. Para PDFs intenta usar primera pagina."""
    try:
        # Intentar abrir como imagen normal
        img = Image.open(io.BytesIO(file_bytes))
        # Convertir a RGB si tiene canal alpha (PNG con transparencia)
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGB")
        # Redimensionar si es muy grande (max 2000px en el lado mayor)
        if max(img.size) > 2000:
            img.thumbnail((2000, 2000))
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        # Si es PDF u otro formato, devolver crudo (OpenAI puede leer PDFs)
        return base64.b64encode(file_bytes).decode()


# ============================================================
# OCR CON OPENAI
# ============================================================

async def ocr_with_openai(image_b64: str, prompt: str, semaphore: asyncio.Semaphore) -> dict:
    """Procesa una imagen con GPT-4o vision."""
    async with semaphore:
        async with httpx.AsyncClient(timeout=120.0) as client:
            try:
                response = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {OPENAI_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "gpt-4o",
                        "messages": [
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": prompt},
                                    {
                                        "type": "image_url",
                                        "image_url": {
                                            "url": f"data:image/png;base64,{image_b64}"
                                        },
                                    },
                                ],
                            }
                        ],
                        "max_tokens": 4000,
                    },
                )
                response.raise_for_status()
                data = response.json()
                text = data["choices"][0]["message"]["content"]
                return parse_ocr_response(text)
            except Exception as e:
                print(f"Error OCR: {e}")
                return {"error": str(e), "results": []}


def parse_ocr_response(text: str) -> dict:
    """Parsea la respuesta del LLM, robusto contra JSON cortado."""
    if not text:
        return {"results": []}
    
    # Limpiar markdown
    cleaned = text.replace("```json", "").replace("```", "").strip()
    
    # Filtrar respuestas de error de OpenAI
    if "I'm sorry" in cleaned or "can't assist" in cleaned:
        return {"results": []}
    
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, list):
            return {"results": parsed}
        elif isinstance(parsed, dict):
            return {"results": [parsed]}
    except json.JSONDecodeError:
        # Cirugia: intentar salvar fragmentos
        no_brackets = cleaned.lstrip("[").rstrip("]")
        fragments = no_brackets.split("},")
        results = []
        for frag in fragments:
            frag = frag.strip()
            if not frag.startswith("{"):
                frag = "{" + frag
            if not frag.endswith("}"):
                frag = frag + "}"
            try:
                results.append(json.loads(frag))
            except:
                pass
        return {"results": results}
    
    return {"results": []}


# ============================================================
# AIRTABLE
# ============================================================

def get_airtable_table(table_name: str):
    api = AirtableApi(AIRTABLE_API_KEY)
    return api.table(AIRTABLE_BASE_ID, table_name)


def fetch_prescripciones_from_airtable() -> list:
    """Trae todas las prescripciones de la tabla actual de Airtable."""
    try:
        table = get_airtable_table(AIRTABLE_TABLE_PRESCRIPCIONES)
        records = table.all()
        return [{**r["fields"], "_airtable_id": r["id"]} for r in records]
    except Exception as e:
        print(f"Error Airtable: {e}")
        return []


def save_auditoria_to_airtable(corte_id: str, corte_nombre: str, resultado: dict) -> Optional[str]:
    """Guarda el resultado de la auditoria en Airtable. Retorna el record_id o None."""
    try:
        table = get_airtable_table(AIRTABLE_TABLE_AUDITORIAS)
        record = table.create({
            "corte_id": corte_id,
            "corte_nombre": corte_nombre,
            "fecha": datetime.now().isoformat(),
            "total_ok": resultado.get("total_ok", 0),
            "total_errores": resultado.get("total_errores", 0),
            "reporte_completo": json.dumps(resultado, ensure_ascii=False, indent=2)[:100000],
        })
        return record["id"]
    except Exception as e:
        print(f"Error guardando auditoria: {e}")
        return None


# ============================================================
# CONSOLIDADOR
# ============================================================

# MAPEO EXACTO DE COLUMNAS DE AIRTABLE
# (Basado en columnas confirmadas por Isabel del Google Sheet original)
COL_NUMERO_PRESCRIPCION = "N° Prescripción"
COL_NOMBRE_PACIENTE = "Nombre del Paciente:"
COL_NOMBRE_ACUDIENTE = "Nombre Acudiente:"
COL_DOCUMENTO = "N° Documento de Identidad Acudiente:"
COL_UBICACION = "Ubicación:"
COL_CANTIDAD = "Cantidad"
COL_PREPARADO_POR = "Preparado por:"
COL_QF = "Nombre y Firma del QF:"
COL_NUMERO_LOTE = "N° Lote:"
COL_FECHA_PREP = "Fecha y Hora de Preparación:"
COL_FECHA_LIMITE = "Fecha Limite de Uso:"
COL_DOSIS = "Dosis"
COL_VIA = "Via de Administración:"


def get_col(record: dict, col_name: str):
    """Obtiene un valor con fallback a comparacion normalizada."""
    if col_name in record:
        return record[col_name]
    target = normalize_text(col_name)
    for k, v in record.items():
        if normalize_text(k) == target:
            return v
    return None


def consolidate(ocr_results: dict, prescripciones: list, corte_id: str) -> dict:
    """
    Recibe resultados de OCR agrupados por tipo + prescripciones de Airtable.
    Cruza, valida y genera el resultado final.
    """
    cajas = ocr_results.get("caja", [])
    frascos = ocr_results.get("frasco", [])
    
    # Agrupar etiquetas por numero_lote
    cajas_por_lote = {}
    frascos_por_lote = {}
    
    for c in cajas:
        lote = (c.get("numero_lote") or "").strip()
        if lote:
            cajas_por_lote.setdefault(lote, []).append(c)
    
    for f in frascos:
        lote = (f.get("numero_lote") or "").strip()
        if lote:
            frascos_por_lote.setdefault(lote, []).append(f)
    
    resultados = []
    lotes_procesados = set()
    
    # 1. Iterar prescripciones oficiales
    for presc in prescripciones:
        lote = get_col(presc, COL_NUMERO_LOTE)
        if not lote:
            continue
        
        lote_key = str(lote).strip()
        lotes_procesados.add(lote_key)
        
        documento = get_col(presc, COL_DOCUMENTO)
        paciente = get_col(presc, COL_NOMBRE_PACIENTE)
        cantidad_raw = get_col(presc, COL_CANTIDAD) or 1
        try:
            cantidad = int(cantidad_raw)
        except (TypeError, ValueError):
            cantidad = 1
        
        numero_prescripcion = get_col(presc, COL_NUMERO_PRESCRIPCION)
        acudiente = get_col(presc, COL_NOMBRE_ACUDIENTE)
        
        cajas_lote = cajas_por_lote.get(lote_key, [])
        frascos_lote = frascos_por_lote.get(lote_key, [])
        
        # Validar campos cruzados
        diferencias = []
        primera_caja = cajas_lote[0] if cajas_lote else None
        
        if primera_caja and documento and similarity(primera_caja.get("documento_acudiente"), str(documento)) < 0.85:
            diferencias.append({
                "campo": "documento_acudiente",
                "valor_etiqueta": primera_caja.get("documento_acudiente"),
                "valor_registro": str(documento),
            })
        
        if primera_caja and paciente and similarity(primera_caja.get("nombre_paciente"), paciente) < 0.75:
            diferencias.append({
                "campo": "nombre_paciente",
                "valor_etiqueta": primera_caja.get("nombre_paciente"),
                "valor_registro": paciente,
            })
        
        if primera_caja and acudiente and similarity(primera_caja.get("nombre_acudiente"), acudiente) < 0.75:
            diferencias.append({
                "campo": "nombre_acudiente",
                "valor_etiqueta": primera_caja.get("nombre_acudiente"),
                "valor_registro": acudiente,
            })
        
        # Determinar estado
        faltan_cajas = len(cajas_lote) < cantidad
        faltan_frascos = len(frascos_lote) < cantidad
        sobran = len(cajas_lote) > cantidad or len(frascos_lote) > cantidad
        
        if not cajas_lote and not frascos_lote:
            estado = "prescripcion_no_en_pdf"
        elif faltan_cajas and faltan_frascos:
            estado = "faltan_ambos"
        elif faltan_cajas:
            estado = "faltan_cajas"
        elif faltan_frascos:
            estado = "faltan_frascos"
        elif sobran:
            estado = "sobran_etiquetas"
        elif diferencias:
            estado = "mismatch_campos"
        else:
            estado = "ok"
        
        resultados.append({
            "numero_lote": lote_key,
            "nombre_paciente": paciente,
            "nombre_acudiente": acudiente,
            "documento_acudiente": documento,
            "numero_prescripcion": numero_prescripcion,
            "cantidad_esperada": cantidad,
            "cajas_detectadas": len(cajas_lote),
            "frascos_detectados": len(frascos_lote),
            "estado": estado,
            "diferencias": diferencias,
        })
    
    # 2. Lotes huerfanos (en PDFs pero no en Airtable)
    todos_lotes_ocr = set(cajas_por_lote.keys()) | set(frascos_por_lote.keys())
    for lote in todos_lotes_ocr:
        if lote in lotes_procesados:
            continue
        cajas_lote = cajas_por_lote.get(lote, [])
        frascos_lote = frascos_por_lote.get(lote, [])
        muestra = (cajas_lote[0] if cajas_lote else frascos_lote[0]) if (cajas_lote or frascos_lote) else {}
        
        resultados.append({
            "numero_lote": lote,
            "nombre_paciente": muestra.get("nombre_paciente"),
            "nombre_acudiente": muestra.get("nombre_acudiente"),
            "documento_acudiente": muestra.get("documento_acudiente"),
            "numero_prescripcion": None,
            "cantidad_esperada": 0,
            "cajas_detectadas": len(cajas_lote),
            "frascos_detectados": len(frascos_lote),
            "estado": "prescripcion_no_en_db",
            "diferencias": [],
        })
    
    # Resumen
    total_ok = sum(1 for r in resultados if r["estado"] == "ok")
    total_errores = sum(1 for r in resultados if r["estado"] != "ok" and r["estado"] != "prescripcion_no_en_db")
    total_huerfanos = sum(1 for r in resultados if r["estado"] == "prescripcion_no_en_db")
    
    return {
        "corte_id": corte_id,
        "fecha": datetime.now().isoformat(),
        "total_prescripciones": len(prescripciones),
        "total_ok": total_ok,
        "total_errores": total_errores,
        "total_huerfanos": total_huerfanos,
        "resultados": resultados,
    }


# ============================================================
# REPORTE
# ============================================================

def build_report_text(corte_nombre: str, resultado: dict) -> str:
    """Construye el reporte en formato texto para WhatsApp."""
    fecha = datetime.now().strftime("%d/%m/%Y %H:%M")
    
    msg = f"📊 *REPORTE DE AUDITORÍA*\n"
    msg += f"🏷 Corte: {corte_nombre}\n"
    msg += f"📅 Fecha: {fecha}\n\n"
    msg += f"📈 *RESUMEN*\n"
    msg += f"✅ Completas: {resultado['total_ok']}\n"
    msg += f"⚠️ Con problemas: {resultado['total_errores']}\n"
    msg += f"🚨 Huérfanos: {resultado['total_huerfanos']}\n"
    msg += f"📋 Total prescripciones: {resultado['total_prescripciones']}\n\n"
    msg += "─" * 30 + "\n"
    
    # Agrupar por estado
    por_estado = {}
    for r in resultado["resultados"]:
        por_estado.setdefault(r["estado"], []).append(r)
    
    # Completas
    if por_estado.get("ok"):
        msg += f"\n✅ *COMPLETAS ({len(por_estado['ok'])})*\n"
        for r in por_estado["ok"]:
            msg += f"• {r.get('nombre_paciente', '(sin nombre)')} · Lote {r['numero_lote']}\n"
            msg += f"  {r['cajas_detectadas']}/{r['cantidad_esperada']} cajas · {r['frascos_detectados']}/{r['cantidad_esperada']} frascos\n"
    
    # Faltantes
    faltantes = (
        por_estado.get("faltan_cajas", []) +
        por_estado.get("faltan_frascos", []) +
        por_estado.get("faltan_ambos", [])
    )
    if faltantes:
        msg += f"\n⚠️ *FALTANTES ({len(faltantes)})*\n"
        for r in faltantes:
            msg += f"• {r.get('nombre_paciente', '(sin nombre)')} · Lote {r['numero_lote']}\n"
            msg += f"  {r['cajas_detectadas']}/{r['cantidad_esperada']} cajas · {r['frascos_detectados']}/{r['cantidad_esperada']} frascos\n"
            if r["cajas_detectadas"] < r["cantidad_esperada"]:
                msg += f"  └ Faltan {r['cantidad_esperada'] - r['cajas_detectadas']} cajas\n"
            if r["frascos_detectados"] < r["cantidad_esperada"]:
                msg += f"  └ Faltan {r['cantidad_esperada'] - r['frascos_detectados']} frascos\n"
    
    # Sobrantes
    if por_estado.get("sobran_etiquetas"):
        msg += f"\n🔺 *SOBRANTES ({len(por_estado['sobran_etiquetas'])})*\n"
        for r in por_estado["sobran_etiquetas"]:
            msg += f"• {r.get('nombre_paciente', '(sin nombre)')} · Lote {r['numero_lote']}\n"
            msg += f"  {r['cajas_detectadas']}/{r['cantidad_esperada']} cajas · {r['frascos_detectados']}/{r['cantidad_esperada']} frascos\n"
    
    # Mismatch
    if por_estado.get("mismatch_campos"):
        msg += f"\n❓ *DIFERENCIAS ({len(por_estado['mismatch_campos'])})*\n"
        for r in por_estado["mismatch_campos"]:
            msg += f"• {r.get('nombre_paciente', '(sin nombre)')} · Lote {r['numero_lote']}\n"
            for d in r["diferencias"]:
                msg += f"  └ {d['campo']}: \"{d['valor_etiqueta']}\" vs \"{d['valor_registro']}\"\n"
    
    # Sin etiquetas
    if por_estado.get("prescripcion_no_en_pdf"):
        msg += f"\n📭 *EN DB SIN ETIQUETAS ({len(por_estado['prescripcion_no_en_pdf'])})*\n"
        for r in por_estado["prescripcion_no_en_pdf"]:
            msg += f"• {r.get('nombre_paciente', '(sin nombre)')} · Lote {r['numero_lote']}\n"
            msg += f"  Esperadas {r['cantidad_esperada']}, detectadas 0\n"
    
    # Huerfanos
    if por_estado.get("prescripcion_no_en_db"):
        msg += f"\n🚨 *HUÉRFANOS - EN PDF SIN PRESCRIPCIÓN ({len(por_estado['prescripcion_no_en_db'])})*\n"
        for r in por_estado["prescripcion_no_en_db"]:
            msg += f"• Lote {r['numero_lote']}\n"
            msg += f"  Paciente: {r.get('nombre_paciente') or '(no detectado)'}\n"
            msg += f"  {r['cajas_detectadas']} cajas · {r['frascos_detectados']} frascos\n"
    
    msg += "\n" + "─" * 30 + "\n"
    msg += "_Klean-Vet® · Auditoría Automatizada_"
    
    return msg


async def send_report_to_n8n(corte_id: str, corte_nombre: str, resultado: dict, report_text: str):
    """Envia el reporte al webhook de n8n separado."""
    if not N8N_REPORT_WEBHOOK_URL:
        print("ADVERTENCIA: N8N_REPORT_WEBHOOK_URL no configurado, saltando envio")
        return
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                N8N_REPORT_WEBHOOK_URL,
                json={
                    "corte_id": corte_id,
                    "corte_nombre": corte_nombre,
                    "report_text": report_text,
                    "summary": {
                        "total_prescripciones": resultado["total_prescripciones"],
                        "total_ok": resultado["total_ok"],
                        "total_errores": resultado["total_errores"],
                        "total_huerfanos": resultado["total_huerfanos"],
                    },
                    "resultados": resultado["resultados"],
                },
            )
            print(f"Reporte enviado a n8n: {response.status_code}")
    except Exception as e:
        print(f"Error enviando a n8n: {e}")


# ============================================================
# WORKFLOW PRINCIPAL
# ============================================================

async def process_corte(corte_id: str, corte_nombre: str, archivos: list):
    """
    Funcion principal: procesa todos los archivos del corte y genera el reporte.
    Corre en background.
    """
    cortes_en_proceso[corte_id] = {
        "estado": "procesando_ocr",
        "corte_nombre": corte_nombre,
        "progreso": {"total": len(archivos), "procesados": 0},
        "iniciado": datetime.now().isoformat(),
    }
    
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_OCR)
    
    # 1. Clasificar archivos por tipo segun nombre
    archivos_por_tipo = {"caja": [], "frasco": [], "prescripcion": [], "envio": [], "desconocido": []}
    for archivo in archivos:
        tipo = classify_filename(archivo["filename"])
        if tipo:
            archivos_por_tipo[tipo].append(archivo)
        else:
            archivos_por_tipo["desconocido"].append(archivo)
    
    print(f"Corte {corte_id}: {len(archivos)} archivos clasificados")
    for tipo, items in archivos_por_tipo.items():
        print(f"  - {tipo}: {len(items)}")
    
    # 2. OCR en paralelo para cada tipo
    async def process_one(archivo, tipo):
        prompt = get_prompt_for_type(tipo)
        b64 = await image_to_base64(archivo["content"], archivo["filename"])
        result = await ocr_with_openai(b64, prompt, semaphore)
        cortes_en_proceso[corte_id]["progreso"]["procesados"] += 1
        return result.get("results", [])
    
    ocr_results = {"caja": [], "frasco": [], "prescripcion": [], "envio": []}
    
    tareas = []
    for tipo in ["caja", "frasco", "prescripcion", "envio"]:
        for archivo in archivos_por_tipo[tipo]:
            tareas.append((tipo, asyncio.create_task(process_one(archivo, tipo))))
    
    for tipo, task in tareas:
        results = await task
        ocr_results[tipo].extend(results)
    
    print(f"Corte {corte_id}: OCR completado")
    print(f"  - Cajas extraidas: {len(ocr_results['caja'])}")
    print(f"  - Frascos extraidos: {len(ocr_results['frasco'])}")
    print(f"  - Prescripciones: {len(ocr_results['prescripcion'])}")
    print(f"  - Envios: {len(ocr_results['envio'])}")
    
    # 3. Traer prescripciones de Airtable
    cortes_en_proceso[corte_id]["estado"] = "consolidando"
    prescripciones = fetch_prescripciones_from_airtable()
    print(f"Corte {corte_id}: {len(prescripciones)} prescripciones traidas de Airtable")
    
    # 4. Consolidar y validar
    resultado = consolidate(ocr_results, prescripciones, corte_id)
    
    # 5. Construir reporte
    report_text = build_report_text(corte_nombre, resultado)
    
    # 6. Guardar en Airtable
    save_auditoria_to_airtable(corte_id, corte_nombre, resultado)
    
    # 7. Enviar a n8n
    await send_report_to_n8n(corte_id, corte_nombre, resultado, report_text)
    
    # 8. Marcar como completado
    cortes_en_proceso[corte_id] = {
        "estado": "completado",
        "corte_nombre": corte_nombre,
        "iniciado": cortes_en_proceso[corte_id]["iniciado"],
        "completado": datetime.now().isoformat(),
        "resultado": resultado,
        "report_text": report_text,
    }
    print(f"Corte {corte_id}: COMPLETADO")


# ============================================================
# ENDPOINTS
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/api/start")
async def start_audit(
    corte_nombre: str = Form(...),
    archivos: list[UploadFile] = File(...),
):
    """Recibe el corte y arranca el procesamiento en background."""
    if not archivos:
        raise HTTPException(400, "No se enviaron archivos")
    
    # Leer todos los archivos a memoria
    archivos_data = []
    for archivo in archivos:
        content = await archivo.read()
        archivos_data.append({
            "filename": archivo.filename,
            "content": content,
        })
    
    corte_id = str(uuid4())
    
    # Disparar procesamiento async sin esperarlo
    asyncio.create_task(process_corte(corte_id, corte_nombre, archivos_data))
    
    return JSONResponse({
        "status": "queued",
        "corte_id": corte_id,
        "corte_nombre": corte_nombre,
        "total_archivos": len(archivos_data),
    })


@app.get("/api/status/{corte_id}")
async def get_status(corte_id: str):
    """Consulta el estado de un corte."""
    if corte_id not in cortes_en_proceso:
        raise HTTPException(404, "Corte no encontrado")
    return cortes_en_proceso[corte_id]


@app.get("/health")
async def health():
    return {"status": "ok", "version": "1.0"}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
