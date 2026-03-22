import asyncio
import os
import sys
import time
from unittest.mock import MagicMock

# Añadir el path del proyecto para poder importar los módulos
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.logger import strategy_logger, executor_logger
from core.db import initialize_db, get_connection
from core.order_model import OrderRequest
from core.config import settings

async def verify_observability():
    print("--- Iniciando Verificación de Observabilidad ---")
    
    # Asegurar que la DB esté inicializada
    initialize_db()
    
    # 1. Simular flujo de Estrategia AI
    print("1. Simulando flujo de IA...")
    with strategy_logger.context() as tid:
        print(f"   [Trace ID: {tid}]")
        strategy_logger.info("ANALYSIS_START", "Iniciando análisis de prueba", {"symbols": ["BTCUSDT", "ETHUSDT"]})
        time.sleep(0.1)
        strategy_logger.info("RAW_RESPONSE", "Simulando respuesta exitosa", {"model": "gpt-4o", "choice": "BUY BTC"})
        
        # Crear OrderRequest con el trace_id
        req = OrderRequest(
            symbol="BTCUSDT",
            side="Buy",
            qty=0.001,
            trace_id=tid,
            entry_price=60000,
            sl_price=59000,
            tp_price=63000
        )
        
        strategy_logger.info("PROPOSAL_READY", "Propuesta generada", {"symbol": "BTCUSDT", "side": "Buy"})

    # 2. Simular flujo de Ejecutor
    print("2. Simulando flujo de Ejecutor...")
    with executor_logger.context(req.trace_id):
        executor_logger.info("ORDER_SENT", "Enviando orden a Bybit", {"qty": req.qty, "price": req.entry_price})
        time.sleep(0.1)
        executor_logger.info("ORDER_SUCCESS", "Orden aceptada", {"orderId": "mock-12345"})

    # 3. Simular Cierre
    print("3. Simulando cierre de posición...")
    with executor_logger.context(req.trace_id):
        executor_logger.info("CLOSING_POSITION", "Cerrando por Take Profit", {"reason": "tp_reached"})
        time.sleep(0.1)
        executor_logger.info("CLOSE_SUCCESS", "Posición cerrada")

    # Esperar un poco a que el worker asíncrono termine de escribir
    print("Esperando 2s para que el worker asíncrono procese la cola...")
    await asyncio.sleep(2)
    
    # 4. Verificar en Base de Datos
    print("4. Verificando en Base de Datos...")
    con = get_connection()
    logs = con.execute("SELECT trace_id, component, event, message FROM system_logs WHERE trace_id = ?", (req.trace_id,)).fetchall()
    con.close()
    
    print(f"   Encontrados {len(logs)} registros para el Trace ID {req.trace_id}")
    for log in logs:
        print(f"   [{log[1]}] {log[2]}: {log[3]}")
    
    if len(logs) >= 6:
        print("\n✅ VERIFICACIÓN EXITOSA: Los logs estructurados se capturaron correctamente.")
    else:
        print(f"\n❌ ERROR: Se esperaban al menos 6 logs, se encontraron {len(logs)}.")

if __name__ == "__main__":
    asyncio.run(verify_observability())
