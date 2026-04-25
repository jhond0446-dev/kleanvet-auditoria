# Klean-Vet · Sistema de Auditoría

Sistema web para auditar etiquetas de cajas, frascos, prescripciones y envíos contra una base de datos en Airtable.

## Cómo funciona

1. Isabel pega los datos del corte en una tabla de Airtable (igual que antes con Google Sheets)
2. Abre la web, escribe el nombre del corte, sube los archivos
3. El sistema usa OpenAI Vision para extraer datos
4. Cruza con Airtable, valida cantidades y campos
5. Genera reporte y lo manda a un webhook de n8n separado para distribución

---

## Setup local (para probar antes de desplegar)

### 1. Instalar Python 3.11+

### 2. Instalar dependencias

```bash
cd kleanvet_python
pip install -r requirements.txt
```

### 3. Configurar variables de entorno

```bash
cp .env.example .env
```

Editar `.env` con tus valores reales:

```
OPENAI_API_KEY=sk-...
AIRTABLE_API_KEY=pat...
AIRTABLE_BASE_ID=app...
AIRTABLE_TABLE_PRESCRIPCIONES=Prescripciones
AIRTABLE_TABLE_AUDITORIAS=Auditorias
N8N_REPORT_WEBHOOK_URL=https://tu-otra-instancia-n8n.com/webhook/kleanvet-reporte
```

### 4. Crear tablas en Airtable

**Tabla `Prescripciones`** (la que Isabel alimenta antes de cada corte):

Mismos campos del Google Sheet original:
- N° Prescripción
- Nombre del Paciente:
- Nombre Acudiente:
- N° Documento de Identidad Acudiente:
- Ubicación:
- Cantidad (numérico)
- Preparado por:
- Nombre y Firma del QF:
- N° Lote:
- Fecha y Hora de Preparación:
- Fecha Limite de Uso:
- Dosis
- Via de Administración:

**Tabla `Auditorias`** (histórico generado automáticamente):

- corte_id (texto)
- corte_nombre (texto)
- fecha (texto)
- total_ok (numérico)
- total_errores (numérico)
- reporte_completo (texto largo)

### 5. Correr local

```bash
python main.py
```

Abre http://localhost:8000

---

## Deploy gratis en Railway

1. Ir a https://railway.app
2. Login con GitHub
3. New Project → Deploy from GitHub repo (subes este código a un repo)
4. Configurar variables de entorno (las del .env)
5. Listo, te da una URL pública

**Tier gratis de Railway: $5 USD de crédito al mes**, suficiente para este uso.

---

## Deploy en Render (alternativa)

1. Ir a https://render.com
2. New Web Service → Connect repo
3. Build Command: `pip install -r requirements.txt`
4. Start Command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. Plan: Free
6. Configurar variables de entorno

**Nota:** Render free duerme tras 15 min de inactividad. Para producción mejor Railway o EasyPanel.

---

## Deploy en EasyPanel del cliente (recomendado para producción)

1. En EasyPanel, crear nuevo servicio tipo "App"
2. Source: Git repo o upload de carpeta
3. Build: usar el `Dockerfile` incluido
4. Configurar variables de entorno
5. Asignar dominio (ej: `auditoria.kleanvet.co`)

---

## Cómo configurar el webhook de n8n para el reporte

En la otra instancia de n8n donde quieras recibir los reportes:

1. Crear workflow nuevo
2. Trigger: Webhook (POST)
3. El webhook va a recibir este JSON:

```json
{
  "corte_id": "uuid",
  "corte_nombre": "CORTE 1 - 1% 15ml",
  "report_text": "📊 *REPORTE DE AUDITORÍA*\n...",
  "summary": {
    "total_prescripciones": 18,
    "total_ok": 15,
    "total_errores": 2,
    "total_huerfanos": 1
  },
  "resultados": [
    {
      "numero_lote": "LC-1-049-03-26 VET",
      "nombre_paciente": "Dala",
      "estado": "ok",
      ...
    }
  ]
}
```

4. Lo conectas a Telegram/WhatsApp/Email/lo que necesites para distribución
5. Pega la URL del webhook en `N8N_REPORT_WEBHOOK_URL` del .env

---

## Variables que puedes ajustar

En el `.env`:

- `MAX_CONCURRENT_OCR`: cuántas imágenes procesar simultáneamente (default 5)
  - Si OpenAI te da rate limit, baja a 3
  - Si tu plan de OpenAI es alto, sube a 10

---

## Estructura de archivos esperada

Los archivos que Isabel sube deben tener una de estas palabras en su nombre:

- `CAJA` → se procesa como etiqueta de caja
- `FRASCO` → se procesa como etiqueta de frasco
- `PRESCRIPCION` o `RECETA` → se procesa como receta médica
- `ENVIO` o `GUIA` → se procesa como guía de envío

Ejemplos válidos:
- `CAJA_001.jpg`
- `frasco_dala_3.png`
- `PRESCRIPCION_picos_y_cachos.pdf`
- `ENVIO_thor.jpg`

---

## Endpoints disponibles

- `GET /` → frontend
- `POST /api/start` → inicia un corte (multipart con archivos + corte_nombre)
- `GET /api/status/{corte_id}` → consulta estado del corte
- `GET /health` → healthcheck
