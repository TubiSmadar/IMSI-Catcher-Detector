#!/usr/bin/env python3
import tkinter as tk
from tkinter import messagebox, scrolledtext, filedialog, ttk
import csv
import subprocess
import threading
import time
import logging
import os
import queue
import signal
import re
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import List, Dict, Optional, Tuple
import json

# Configuration
IMSI_DB = None  # Will hold the path to CSV after upload
LOG_FILE = os.path.expanduser("~/imsi_scanner.log")
MAX_HOP_COUNT = 1  # Maximum allowed hop count to avoid becoming "the antenna"
SCAN_TIMEOUT = 6000000000000000 # Timeout for each frequency scan in seconds

# Global state
discovered_frequencies = []
manual_frequencies = []
active_monitors = {}
scan_results = []
TARGET_IMSI = None  # Global target IMSI

log_queue = queue.Queue()

class QueueHandler(logging.Handler):
    def emit(self, record):
        log_entry = self.format(record)
        log_queue.put(log_entry)

# Logger setup
logger = logging.getLogger("IMSI_GUI")
logger.setLevel(logging.DEBUG)
file_handler = logging.FileHandler(LOG_FILE)
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
queue_handler = QueueHandler()
queue_handler.setLevel(logging.INFO)
queue_handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
logger.addHandler(file_handler)
logger.addHandler(queue_handler)

logger.info("=== IMSI Scanner GUI Started (Async Edition) ===")

class FrequencyScanner:
    """Handles frequency discovery operations"""
    
    def __init__(self):
        self.scanning = False
        self.scan_task = None
        
    async def scan_band_async(self, band: str) -> List[str]:
        """Asynchronously scan a single band for frequencies"""
        logger.info(f"[FREQ_SCANNER] Starting async scan of band: {band}")
        frequencies = []
        
        try:
            # Run grgsm_scanner and capture output
            process = await asyncio.create_subprocess_exec(
                'grgsm_scanner', '-b', band,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            # Multiple possible patterns for frequency detection
            patterns = [
                re.compile(r"Freq:\s+([\d.]+)M", re.IGNORECASE),
                re.compile(r"(\d{3,4}\.\d+)M", re.IGNORECASE),
                re.compile(r"ARFCN:\s*\d+\s*Freq:\s*([\d.]+)M", re.IGNORECASE),
                re.compile(r"Found:\s*([\d.]+)M", re.IGNORECASE)
            ]
            
            # Read all output at once with a proper timeout
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), 
                    timeout=30.0  # Give scanner 30 seconds to complete
                )
                
                # Process stdout
                if stdout:
                    output = stdout.decode('utf-8', errors='ignore')
                    logger.debug(f"[FREQ_SCANNER] Raw output from {band}:\n{output[:500]}")  # Log first 500 chars
                    
                    # Try each line
                    for line in output.split('\n'):
                        line = line.strip()
                        if line:
                            # Try all patterns
                            for pattern in patterns:
                                match = pattern.search(line)
                                if match:
                                    freq = f"{match.group(1)}M"
                                    if freq not in frequencies:  # Avoid duplicates
                                        frequencies.append(freq)
                                        logger.info(f"[FREQ_SCANNER] ✓ Found: {freq} in {band}")
                                        break
                
                # Also check stderr in case output goes there
                if stderr:
                    err_output = stderr.decode('utf-8', errors='ignore')
                    logger.debug(f"[FREQ_SCANNER] Stderr from {band}: {err_output[:200]}")
                    
            except asyncio.TimeoutError:
                logger.warning(f"[FREQ_SCANNER] Timeout scanning {band} - terminating")
                process.terminate()
                await asyncio.sleep(0.5)
                if process.returncode is None:
                    process.kill()
                    
        except FileNotFoundError:
            logger.error("[FREQ_SCANNER] grgsm_scanner not found. Please ensure gr-gsm is installed.")
            messagebox.showerror("Scanner Not Found", 
                               "grgsm_scanner not found.\n"
                               "Please install gr-gsm:\n"
                               "sudo apt-get install gr-gsm")
        except Exception as e:
            logger.error(f"[FREQ_SCANNER] Error scanning {band}: {e}")
            
        logger.info(f"[FREQ_SCANNER] Band {band} scan complete: found {len(frequencies)} frequencies")
        return frequencies
    
    async def scan_all_bands(self, progress_callback=None) -> List[str]:
        """Scan all GSM bands asynchronously - with fallback to synchronous scanning"""
        logger.info("[FREQ_SCANNER] === FREQUENCY DISCOVERY STARTED ===")
        bands = ['GSM900', 'DCS1800', 'GSM850', 'PCS1900']
        all_frequencies = []
        
        # Try async scanning first
        for i, band in enumerate(bands):
            if progress_callback:
                await asyncio.get_event_loop().run_in_executor(
                    None, progress_callback, f"Scanning {band}...", int((i/len(bands)) * 100)
                )
            
            frequencies = await self.scan_band_async(band)
            all_frequencies.extend(frequencies)
        
        # If async scanning found nothing, try synchronous fallback
        if not all_frequencies:
            logger.warning("[FREQ_SCANNER] Async scan found nothing, trying synchronous fallback...")
            all_frequencies = await self.scan_bands_sync_fallback(bands, progress_callback)
        
        # Remove duplicates
        unique_frequencies = list(set(all_frequencies))
        unique_frequencies.sort(key=lambda x: float(x.replace('M', '')))  # Sort numerically
        
        logger.info(f"[FREQ_SCANNER] === DISCOVERY COMPLETE: {len(unique_frequencies)} frequencies found ===")
        if unique_frequencies:
            logger.info(f"[FREQ_SCANNER] Frequencies: {unique_frequencies}")
        
        if progress_callback:
            await asyncio.get_event_loop().run_in_executor(
                None, progress_callback, f"Found {len(unique_frequencies)} frequencies", 100
            )
        
        return unique_frequencies
    
    async def scan_bands_sync_fallback(self, bands: List[str], progress_callback=None) -> List[str]:
        """Synchronous fallback for frequency scanning"""
        logger.info("[FREQ_SCANNER] Using synchronous fallback method")
        all_frequencies = []
        
        for i, band in enumerate(bands):
            if progress_callback:
                await asyncio.get_event_loop().run_in_executor(
                    None, progress_callback, f"Fallback scanning {band}...", int((i/len(bands)) * 100)
                )
            
            try:
                # Run synchronously with direct subprocess
                result = subprocess.run(
                    ['grgsm_scanner', '-b', band],
                    capture_output=True,
                    text=True,
                    timeout=30
                )
                
                output = result.stdout + result.stderr
                logger.debug(f"[FREQ_SCANNER] Fallback output for {band}:\n{output[:500]}")
                
                # Try multiple patterns
                patterns = [
                    r"Freq:\s+([\d.]+)M",
                    r"(\d{3,4}\.\d+)M",
                    r"ARFCN:\s*\d+\s*Freq:\s*([\d.]+)M",
                    r"freq\s*=\s*([\d.]+)M",
                    r"Center Frequency:\s*([\d.]+)M"
                ]
                
                for pattern in patterns:
                    matches = re.findall(pattern, output, re.IGNORECASE)
                    for match in matches:
                        freq = f"{match}M" if not match.endswith('M') else match
                        if freq not in all_frequencies:
                            all_frequencies.append(freq)
                            logger.info(f"[FREQ_SCANNER] ✓ Fallback found: {freq} in {band}")
                
            except subprocess.TimeoutExpired:
                logger.warning(f"[FREQ_SCANNER] Fallback timeout for {band}")
            except Exception as e:
                logger.error(f"[FREQ_SCANNER] Fallback error for {band}: {e}")
        
        return all_frequencies

class IMSIMonitor:
    """Handles IMSI monitoring operations"""
    
    def __init__(self):
        self.active_monitors = {}
        self.executor = ThreadPoolExecutor(max_workers=4)
        
    async def monitor_frequency_async(self, freq: str, target_imsi: str, 
                                     duration: int = SCAN_TIMEOUT) -> Optional[Dict]:
        """Asynchronously monitor a single frequency for IMSI"""
        freq_hz = freq.replace('M', '000000')
        logger.info(f"[IMSI_MONITOR] Starting monitor on {freq} for IMSI {target_imsi}")
        
        monitor_id = f"{freq}_{target_imsi}_{time.time()}"
        self.active_monitors[monitor_id] = {'status': 'running', 'frequency': freq}
        
        try:
            # Start grgsm_livemon
            livemon_proc = await asyncio.create_subprocess_exec(
                'grgsm_livemon', f'--fc={freq_hz}', '--no-gui', '--ppm=0',
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL
            )
            
            # Start tshark monitoring
            result = await self.capture_imsi_async(target_imsi, freq, duration)
            
            # Cleanup
            livemon_proc.terminate()
            await livemon_proc.wait()
            
            if result:
                logger.info(f"[IMSI_MONITOR] ✓✓✓ IMSI FOUND on {freq}!")
                self.active_monitors[monitor_id]['status'] = 'found'
                return result
            else:
                logger.info(f"[IMSI_MONITOR] No IMSI found on {freq}")
                self.active_monitors[monitor_id]['status'] = 'not_found'
                
        except Exception as e:
            logger.error(f"[IMSI_MONITOR] Error monitoring {freq}: {e}")
            self.active_monitors[monitor_id]['status'] = 'error'
            
        finally:
            # Remove from active monitors after delay
            await asyncio.sleep(1)
            if monitor_id in self.active_monitors:
                del self.active_monitors[monitor_id]
                
        return None
    
    async def capture_imsi_async(self, target_imsi: str, freq: str, 
                                 duration: int) -> Optional[Dict]:
        """Capture IMSI with routing detection"""
        logger.info(f"[TSHARK] Starting capture for {target_imsi} (max {duration}s)")
        
        tshark_cmd = [
            'sudo', 'tshark', '-i', 'lo',
            '-Y', f'e212.imsi == {target_imsi}',
            '-T', 'fields',
            '-e', 'frame.time',
            '-e', 'e212.imsi',
            '-e', 'frame.number',
            '-e', 'gsm_a.rr.ho_ref',
            '-e', 'gsm_a.tmsi',
            '-e', 'gsm_map.old_lac',
            '-e', 'gsm_map.lac',
            '-e', 'ip.ttl',
            '-e', 'frame.len',
            '-e', 'gsm_a.rr.rxlev_full_serv_cell',
            '-l', '-a', f'duration:{duration}'
        ]
        
        try:
            process = await asyncio.create_subprocess_exec(
                *tshark_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL
            )
            
            detection_count = 0
            start_time = time.time()
            
            while time.time() - start_time < duration:
                try:
                    line = await asyncio.wait_for(
                        process.stdout.readline(),
                        timeout=0.5
                    )
                    
                    if not line:
                        continue
                        
                    line = line.decode().strip()
                    if line:
                        fields = line.split('\t')
                        detection_count += 1
                        
                        logger.info(f"[TSHARK] Detection #{detection_count} on {freq}: {line}")
                        
                        # Analyze routing indicators
                        is_direct, routing_info = self.analyze_routing(fields, detection_count)
                        
                        if routing_info:
                            logger.warning(f"[TSHARK] Routing indicators: {routing_info}")
                        
                        if is_direct:
                            logger.info(f"[TSHARK] ✓ Direct transmission confirmed!")
                            await process.terminate()
                            await process.wait()
                            
                            return {
                                'timestamp': datetime.now().isoformat(),
                                'imsi': target_imsi,
                                'frequency': freq,
                                'direct': True,
                                'detection_count': detection_count,
                                'routing_info': routing_info
                            }
                        else:
                            logger.warning(f"[TSHARK] ⚠ Routed/repeated signal - continuing search")
                            
                except asyncio.TimeoutError:
                    continue
                    
            await process.terminate()
            await process.wait()
            
        except Exception as e:
            logger.error(f"[TSHARK] Capture error: {e}")
            
        return None
    
    def analyze_routing(self, fields: List[str], detection_count: int) -> Tuple[bool, str]:
        """Analyze if transmission is direct or routed"""
        is_direct = True
        routing_indicators = []
        
        # Check handover reference
        if len(fields) > 3 and fields[3]:
            routing_indicators.append("Handover detected")
            is_direct = False
        
        # Check LAC changes
        if len(fields) > 6:
            old_lac = fields[5] if fields[5] else None
            new_lac = fields[6] if fields[6] else None
            if old_lac and new_lac and old_lac != new_lac:
                routing_indicators.append(f"LAC change: {old_lac}→{new_lac}")
                is_direct = False
        
        # Check TTL for hop count
        if len(fields) > 7 and fields[7] and fields[7].isdigit():
            ttl = int(fields[7])
            if ttl < 62:  # Initial TTL is typically 64
                hop_count = 64 - ttl
                if hop_count > MAX_HOP_COUNT:
                    routing_indicators.append(f"Hop count: {hop_count}")
                    is_direct = False
        
        # Multiple rapid detections might indicate routing
        if detection_count > 3:
            routing_indicators.append(f"Multiple detections: {detection_count}")
            
        return is_direct, ", ".join(routing_indicators) if routing_indicators else None

class ScanController:
    """Main controller for scan operations"""
    
    def __init__(self):
        self.freq_scanner = FrequencyScanner()
        self.imsi_monitor = IMSIMonitor()
        self.loop = None
        self.scan_task = None
        self.monitor_tasks = []
        self.stop_event = asyncio.Event()
        
    def start_async_loop(self):
        """Start the async event loop in a separate thread"""
        if self.loop and self.loop.is_running():
            logger.warning("Async loop already running")
            return
            
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_forever()
        except Exception as e:
            logger.error(f"Async loop error: {e}")
        finally:
            self.loop.close()
            self.loop = None
        
    async def discover_frequencies(self, progress_callback=None):
        """Discover available frequencies"""
        global discovered_frequencies
        discovered_frequencies = await self.freq_scanner.scan_all_bands(progress_callback)
        return discovered_frequencies
    
    async def monitor_frequencies(self, frequencies: List[str], target_imsi: str, 
                                 continuous: bool = True):
        """Monitor multiple frequencies for IMSI - ONLY monitors the provided frequencies"""
        logger.info(f"[CONTROLLER] Starting targeted monitoring of {len(frequencies)} specific frequencies")
        logger.info(f"[CONTROLLER] Frequencies to monitor: {frequencies}")
        
        scan_cycle = 0
        while not self.stop_event.is_set():
            scan_cycle += 1
            logger.info(f"[CONTROLLER] === Monitoring Cycle #{scan_cycle} ===")
            tasks = []
            
            for freq in frequencies:
                if self.stop_event.is_set():
                    break
                logger.info(f"[CONTROLLER] Queuing monitor for frequency: {freq}")
                task = asyncio.create_task(
                    self.imsi_monitor.monitor_frequency_async(freq, target_imsi)
                )
                tasks.append(task)
                
            logger.info(f"[CONTROLLER] Executing {len(tasks)} parallel monitors...")
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            for freq, result in zip(frequencies, results):
                if isinstance(result, dict):
                    # IMSI found!
                    logger.info(f"[CONTROLLER] ✓✓✓ TARGET FOUND on {freq}")
                    scan_results.append(result)
                    return result
                elif isinstance(result, Exception):
                    logger.error(f"[CONTROLLER] Error monitoring {freq}: {result}")
                    
            if not continuous:
                logger.info("[CONTROLLER] Single scan complete, stopping")
                break
                
            if not self.stop_event.is_set():
                logger.info(f"[CONTROLLER] Cycle #{scan_cycle} complete. Restarting in 2 seconds...")
                await asyncio.sleep(2)
            
        logger.info("[CONTROLLER] Monitoring stopped")
        return None
    
    def stop_all(self):
        """Stop all scanning operations"""
        logger.info("[CONTROLLER] Stopping all operations")
        self.stop_event.set()
        
        if self.scan_task:
            self.scan_task.cancel()
            
        for task in self.monitor_tasks:
            task.cancel()

# Database functions
def get_imsi_by_phone(phone: str) -> Optional[str]:
    """Lookup IMSI by phone number"""
    if IMSI_DB is None:
        logger.warning("No CSV database loaded")
        return None
        
    logger.info(f"Looking up IMSI for phone: {phone}")
    try:
        with open(IMSI_DB, newline='') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get('phone', '').strip() == phone:
                    imsi = row.get('imsi', '')
                    logger.info(f"Found IMSI: {imsi}")
                    return imsi
        logger.warning(f"No IMSI found for phone: {phone}")
    except Exception as e:
        logger.error(f"Error reading CSV: {e}")
    return None

# Create controller instance
controller = ScanController()

# === Define all GUI callback functions BEFORE creating GUI ===

def run_async_task(coro):
    """Run async task in the event loop"""
    if not controller.loop or not controller.loop.is_running():
        logger.error("Async loop not running - starting it now")
        # Restart the async loop if needed
        async_thread = threading.Thread(target=controller.start_async_loop, daemon=True)
        async_thread.start()
        time.sleep(0.5)  # Give it time to start
        
    if controller.loop and controller.loop.is_running():
        future = asyncio.run_coroutine_threadsafe(coro, controller.loop)
        return future
    else:
        logger.error("Failed to start async loop")
        messagebox.showerror("Error", "Failed to initialize async system")
        return None

def set_target_imsi():
    """Set the target IMSI directly from the entry field"""
    global TARGET_IMSI
    imsi = imsi_entry.get().strip()
    
    if not imsi:
        messagebox.showwarning("Invalid Input", "Please enter a valid IMSI")
        return
    
    # Basic IMSI validation (should be 15 digits)
    if not imsi.isdigit() or len(imsi) < 14 or len(imsi) > 15:
        messagebox.showwarning("Invalid IMSI", "IMSI should be 14-15 digits")
        return
    
    TARGET_IMSI = imsi
    current_target_var.set(f"IMSI: {TARGET_IMSI}")
    current_target_label.config(foreground="green")
    logger.info(f"[TARGET] Target IMSI set: {TARGET_IMSI}")
    messagebox.showinfo("Success", f"Target IMSI set to: {TARGET_IMSI}")

def lookup_phone_number():
    """Lookup IMSI from phone number using CSV database"""
    global TARGET_IMSI
    phone = phone_entry.get().strip()
    
    if not phone:
        messagebox.showwarning("Invalid Input", "Please enter a phone number")
        return
    
    if IMSI_DB is None:
        messagebox.showwarning("No Database", "Please upload CSV database first")
        return
    
    imsi = get_imsi_by_phone(phone)
    if imsi:
        TARGET_IMSI = imsi
        current_target_var.set(f"IMSI: {TARGET_IMSI} (Phone: {phone})")
        current_target_label.config(foreground="green")
        logger.info(f"[TARGET] Target IMSI set from phone lookup: {TARGET_IMSI}")
        messagebox.showinfo("Success", f"Found IMSI: {TARGET_IMSI}\nFor phone: {phone}")
    else:
        messagebox.showerror("Not Found", f"No IMSI found for phone: {phone}")

def upload_csv():
    """Upload IMSI-Phone CSV database"""
    global IMSI_DB
    
    file_path = filedialog.askopenfilename(
        title="Select IMSI-Phone CSV",
        filetypes=[("CSV files", "*.csv"), ("All files", "*.*")]
    )
    
    if file_path:
        IMSI_DB = file_path
        try:
            with open(IMSI_DB, newline='') as f:
                reader = csv.DictReader(f)
                count = sum(1 for row in reader)
                
            logger.info(f"[CSV] Loaded database: {count} entries")
            csv_status_label.config(text=f"✓ {count} entries loaded")
            messagebox.showinfo("Success", f"Loaded {count} entries from CSV\nYou can now use 'Lookup Phone' feature")
            
        except Exception as e:
            logger.error(f"[CSV] Error: {e}")
            messagebox.showerror("Error", f"Failed to load CSV: {e}")
            IMSI_DB = None

def on_frequency_discovery():
    """Handle frequency discovery button click"""
    logger.info("[GUI] Starting frequency discovery...")
    
    # Ensure async loop is running
    if not controller.loop or not controller.loop.is_running():
        logger.warning("[GUI] Async loop not running, starting it...")
        async_thread = threading.Thread(target=controller.start_async_loop, daemon=True)
        async_thread.start()
        time.sleep(0.5)  # Give it time to start
    
    def progress_update(msg, percent):
        freq_status_label.config(text=msg)
        freq_progress_bar['value'] = percent
        
    async def discover():
        try:
            frequencies = await controller.discover_frequencies(progress_update)
            
            # Update GUI with results
            freq_listbox.delete(0, tk.END)
            for freq in frequencies:
                freq_listbox.insert(tk.END, freq)
                
            freq_status_label.config(text=f"Found {len(frequencies)} frequencies")
            messagebox.showinfo("Discovery Complete", 
                              f"Found {len(frequencies)} active frequencies")
        except Exception as e:
            logger.error(f"[GUI] Discovery error: {e}")
            messagebox.showerror("Discovery Error", f"Failed to discover frequencies: {e}")
        finally:
            freq_discover_btn.config(state='normal')
        
    freq_discover_btn.config(state='disabled')
    future = run_async_task(discover())
    if not future:
        freq_discover_btn.config(state='normal')
        messagebox.showerror("Error", "Failed to start frequency discovery")

def on_manual_frequency_add():
    """Add manual frequency"""
    freq = manual_freq_entry.get().strip()
    
    if not freq:
        messagebox.showwarning("Invalid Input", "Please enter a frequency")
        return
        
    # Validate format (should be like "935.2M" or "935M")
    if not re.match(r'^\d+(\.\d+)?M$', freq):
        messagebox.showwarning("Invalid Format", "Frequency should be in format: 935.2M")
        return
        
    if freq not in manual_frequencies:
        manual_frequencies.append(freq)
        freq_listbox.insert(tk.END, f"[M] {freq}")
        logger.info(f"[GUI] Added manual frequency: {freq}")
        manual_freq_entry.delete(0, tk.END)
    else:
        messagebox.showinfo("Duplicate", "Frequency already in list")

def on_clear_frequencies():
    """Clear frequency list"""
    freq_listbox.delete(0, tk.END)
    global discovered_frequencies, manual_frequencies
    discovered_frequencies = []
    manual_frequencies = []
    logger.info("[GUI] Frequency list cleared")

def on_start_monitoring():
    """Start IMSI monitoring on selected frequencies or auto-discover if none available"""
    global TARGET_IMSI
    
    # Check if target IMSI is set
    if not TARGET_IMSI:
        messagebox.showerror("No Target", "Please set target IMSI first!\nUse 'Set IMSI' or 'Lookup Phone' button")
        return
        
    # Get selected frequencies from the listbox
    selected_indices = freq_listbox.curselection()
    selected_freqs = []
    auto_scan = False
    
    if selected_indices:
        # User has selected specific frequencies
        for i in selected_indices:
            freq = freq_listbox.get(i)
            # Remove [M] prefix if manual frequency
            if freq.startswith("[M] "):
                freq = freq[4:]
            selected_freqs.append(freq)
        logger.info(f"[GUI] Using selected frequencies: {selected_freqs}")
        monitor_status_label.config(text=f"Monitoring {len(selected_freqs)} selected frequencies for {TARGET_IMSI}...")
        
    elif freq_listbox.size() > 0:
        # No selection but frequencies exist in list - ask user
        response = messagebox.askyesno(
            "No Selection", 
            "No frequencies selected.\n\nDo you want to monitor ALL frequencies in the list?\n\n"
            "Yes = Monitor all listed frequencies\n"
            "No = Auto-scan all GSM bands"
        )
        if response:
            # Monitor all frequencies in the list
            for i in range(freq_listbox.size()):
                freq = freq_listbox.get(i)
                if freq.startswith("[M] "):
                    freq = freq[4:]
                selected_freqs.append(freq)
            logger.info(f"[GUI] Using all listed frequencies: {selected_freqs}")
            monitor_status_label.config(text=f"Monitoring ALL {len(selected_freqs)} listed frequencies...")
        else:
            auto_scan = True
    else:
        # No frequencies at all - auto-scan
        response = messagebox.askyesno(
            "No Frequencies Available",
            "No frequencies discovered or added.\n\n"
            "Do you want to auto-scan all GSM bands?\n"
            "This will discover frequencies and then monitor them."
        )
        if response:
            auto_scan = True
        else:
            return
    
    if auto_scan:
        # Auto-discover and monitor
        logger.info("[GUI] Auto-scanning all GSM bands for frequencies...")
        monitor_status_label.config(text="Auto-discovering frequencies...")
        monitor_start_btn.config(text="Stop Monitoring", command=on_stop_monitoring)
        
        async def auto_discover_and_monitor():
            try:
                # First, discover frequencies
                logger.info("[GUI] Starting automatic frequency discovery...")
                frequencies = await controller.discover_frequencies()
                
                if not frequencies:
                    logger.error("[GUI] No frequencies found during auto-scan")
                    messagebox.showerror("No Frequencies", "Could not find any active frequencies")
                    monitor_status_label.config(text="Failed - no frequencies found")
                    monitor_start_btn.config(text="Start Monitoring", command=on_start_monitoring)
                    return
                
                # Update the frequency list in GUI
                freq_listbox.delete(0, tk.END)
                for freq in frequencies:
                    freq_listbox.insert(tk.END, freq)
                
                logger.info(f"[GUI] Auto-discovered {len(frequencies)} frequencies: {frequencies}")
                monitor_status_label.config(text=f"Auto-monitoring {len(frequencies)} discovered frequencies...")
                
                # Now monitor all discovered frequencies
                result = await controller.monitor_frequencies(frequencies, TARGET_IMSI, continuous=True)
                
                if result:
                    # IMSI found!
                    messagebox.showinfo(
                        "IMSI Detected!",
                        f"IMSI: {result['imsi']}\n"
                        f"Frequency: {result['frequency']}\n"
                        f"Time: {result['timestamp']}\n"
                        f"Direct: {result['direct']}\n"
                        f"Detections: {result['detection_count']}"
                    )
                    
                    # Add to results list
                    result_text = f"[{result['timestamp']}] {result['imsi']} on {result['frequency']}"
                    results_listbox.insert(0, result_text)
                    monitor_status_label.config(text=f"IMSI Found on {result['frequency']}!")
                else:
                    monitor_status_label.config(text="Monitoring stopped")
                    
            except Exception as e:
                logger.error(f"[GUI] Auto-discover error: {e}")
                messagebox.showerror("Error", f"Auto-discovery failed: {e}")
                monitor_status_label.config(text="Error during auto-discovery")
            finally:
                monitor_start_btn.config(text="Start Monitoring", command=on_start_monitoring)
        
        future = run_async_task(auto_discover_and_monitor())
        if not future:
            monitor_start_btn.config(text="Start Monitoring", command=on_start_monitoring)
            messagebox.showerror("Error", "Failed to start auto-discovery")
    else:
        # Monitor selected frequencies
        monitor_start_btn.config(text="Stop Monitoring", command=on_stop_monitoring)
        
        async def monitor():
            try:
                result = await controller.monitor_frequencies(selected_freqs, TARGET_IMSI, continuous=True)
                if result:
                    # IMSI found!
                    messagebox.showinfo(
                        "IMSI Detected!",
                        f"IMSI: {result['imsi']}\n"
                        f"Frequency: {result['frequency']}\n"
                        f"Time: {result['timestamp']}\n"
                        f"Direct: {result['direct']}\n"
                        f"Detections: {result['detection_count']}"
                    )
                    
                    # Add to results list
                    result_text = f"[{result['timestamp']}] {result['imsi']} on {result['frequency']}"
                    results_listbox.insert(0, result_text)
                    monitor_status_label.config(text=f"IMSI Found on {result['frequency']}!")
                else:
                    monitor_status_label.config(text="Monitoring stopped")
            except Exception as e:
                logger.error(f"[GUI] Monitor error: {e}")
                messagebox.showerror("Error", f"Monitoring failed: {e}")
                monitor_status_label.config(text="Error during monitoring")
            finally:
                monitor_start_btn.config(text="Start Monitoring", command=on_start_monitoring)
            
        future = run_async_task(monitor())
        if not future:
            monitor_start_btn.config(text="Start Monitoring", command=on_start_monitoring)
            messagebox.showerror("Error", "Failed to start monitoring")

def on_stop_monitoring():
    """Stop IMSI monitoring"""
    logger.info("[GUI] Stopping monitoring...")
    controller.stop_all()
    
    monitor_status_label.config(text="Monitoring stopped")
    monitor_start_btn.config(text="Start Monitoring", command=on_start_monitoring)

def test_manual_scanner():
    """Test function to manually run grgsm_scanner and show output"""
    logger.info("[TEST] Running manual grgsm_scanner test...")
    
    test_window = tk.Toplevel(root)
    test_window.title("Scanner Test")
    test_window.geometry("600x400")
    
    test_text = scrolledtext.ScrolledText(test_window, width=70, height=20)
    test_text.pack(padx=10, pady=10)
    
    def run_test():
        test_text.insert(tk.END, "Running: grgsm_scanner -b GSM900\n")
        test_text.insert(tk.END, "=" * 50 + "\n")
        
        try:
            process = subprocess.Popen(
                ['grgsm_scanner', '-b', 'GSM900'],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True
            )
            
            for line in process.stdout:
                test_text.insert(tk.END, line)
                test_text.see(tk.END)
                test_window.update()
                
                # Look for frequencies
                if "Freq:" in line or "MHz" in line or "ARFCN" in line:
                    test_text.insert(tk.END, f">>> DETECTED: {line}")
                    
            process.wait()
            test_text.insert(tk.END, "\n" + "=" * 50 + "\n")
            test_text.insert(tk.END, "Scan complete!\n")
            
        except Exception as e:
            test_text.insert(tk.END, f"Error: {e}\n")
    
    test_btn = ttk.Button(test_window, text="Run Test Scan", command=run_test)
    test_btn.pack(pady=5)

def update_log_display():
    """Update the log display with queued messages"""
    while not log_queue.empty():
        msg = log_queue.get()
        log_box.config(state='normal')
        log_box.insert(tk.END, msg + '\n')
        log_box.see(tk.END)
        log_box.config(state='disabled')
        
    # Update active monitors display
    if hasattr(controller.imsi_monitor, 'active_monitors'):
        active_text.delete(1.0, tk.END)
        for monitor_id, info in controller.imsi_monitor.active_monitors.items():
            active_text.insert(tk.END, f"[{info['status']}] {info['frequency']}\n")
            
    root.after(100, update_log_display)

def on_closing():
    """Clean shutdown"""
    logger.info("=== Shutting down IMSI Scanner ===")
    controller.stop_all()
    if controller.loop and controller.loop.is_running():
        controller.loop.call_soon_threadsafe(controller.loop.stop)
    root.destroy()

# === GUI Setup ===
root = tk.Tk()
root.title("IMSI Scanner - Async Edition")
root.geometry("1000x750")

# Create notebook for tabs
notebook = ttk.Notebook(root)
notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

# === Tab 1: Configuration ===
config_tab = ttk.Frame(notebook)
notebook.add(config_tab, text="Configuration")

# Target configuration
target_frame = ttk.LabelFrame(config_tab, text="Target Configuration", padding=10)
target_frame.pack(fill=tk.X, padx=10, pady=10)

# Current target display
current_target_var = tk.StringVar(value="No target set")
ttk.Label(target_frame, text="Current Target:", font=('Arial', 10, 'bold')).grid(row=0, column=0, sticky="w", pady=5)
current_target_label = ttk.Label(target_frame, textvariable=current_target_var, foreground="red")
current_target_label.grid(row=0, column=1, columnspan=2, sticky="w", pady=5)

ttk.Separator(target_frame, orient='horizontal').grid(row=1, column=0, columnspan=3, sticky="ew", pady=10)

# Direct IMSI entry
ttk.Label(target_frame, text="Enter IMSI:").grid(row=2, column=0, sticky="w", pady=5)
imsi_entry = ttk.Entry(target_frame, width=30)
imsi_entry.grid(row=2, column=1, pady=5, padx=5)

set_imsi_btn = ttk.Button(target_frame, text="Set IMSI", command=lambda: set_target_imsi())
set_imsi_btn.grid(row=2, column=2, pady=5, padx=5)

# Phone number entry (with CSV lookup)
ttk.Label(target_frame, text="Enter Phone:").grid(row=3, column=0, sticky="w", pady=5)
phone_entry = ttk.Entry(target_frame, width=30)
phone_entry.grid(row=3, column=1, pady=5, padx=5)

lookup_phone_btn = ttk.Button(target_frame, text="Lookup Phone", command=lambda: lookup_phone_number())
lookup_phone_btn.grid(row=3, column=2, pady=5, padx=5)

ttk.Separator(target_frame, orient='horizontal').grid(row=4, column=0, columnspan=3, sticky="ew", pady=10)

# CSV Database section
csv_frame = ttk.Frame(target_frame)
csv_frame.grid(row=5, column=0, columnspan=3, pady=5)

upload_btn = ttk.Button(csv_frame, text="Upload CSV Database", command=upload_csv)
upload_btn.pack(side=tk.LEFT, padx=5)

csv_status_label = ttk.Label(csv_frame, text="No CSV loaded", foreground="gray")
csv_status_label.pack(side=tk.LEFT, padx=10)

# === Tab 2: Frequency Discovery ===
freq_tab = ttk.Frame(notebook)
notebook.add(freq_tab, text="Frequency Management")

# Discovery section
discovery_frame = ttk.LabelFrame(freq_tab, text="Frequency Discovery", padding=10)
discovery_frame.pack(fill=tk.X, padx=10, pady=10)

freq_discover_btn = ttk.Button(discovery_frame, text="Discover Frequencies", 
                               command=on_frequency_discovery)
freq_discover_btn.pack(pady=5)

# Add test button to the frequency discovery frame
test_scanner_btn = ttk.Button(discovery_frame, text="Test Scanner Output", 
                              command=test_manual_scanner)
test_scanner_btn.pack(pady=5)

freq_progress_bar = ttk.Progressbar(discovery_frame, mode='determinate', length=400)
freq_progress_bar.pack(pady=5)

freq_status_label = ttk.Label(discovery_frame, text="Ready to scan", foreground="blue")
freq_status_label.pack(pady=5)

# Manual frequency section
manual_frame = ttk.LabelFrame(freq_tab, text="Manual Frequency Entry", padding=10)
manual_frame.pack(fill=tk.X, padx=10, pady=10)

ttk.Label(manual_frame, text="Frequency (e.g., 935.2M):").pack(side=tk.LEFT, padx=5)
manual_freq_entry = ttk.Entry(manual_frame, width=15)
manual_freq_entry.pack(side=tk.LEFT, padx=5)

add_freq_btn = ttk.Button(manual_frame, text="Add Frequency", 
                          command=on_manual_frequency_add)
add_freq_btn.pack(side=tk.LEFT, padx=5)

# Frequency list
list_frame = ttk.LabelFrame(freq_tab, text="Available Frequencies", padding=10)
list_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

# Scrollbar for listbox
scrollbar = ttk.Scrollbar(list_frame)
scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

freq_listbox = tk.Listbox(list_frame, selectmode=tk.MULTIPLE, 
                          yscrollcommand=scrollbar.set, height=10)
freq_listbox.pack(fill=tk.BOTH, expand=True)
scrollbar.config(command=freq_listbox.yview)

# List controls
list_control_frame = ttk.Frame(list_frame)
list_control_frame.pack(fill=tk.X, pady=5)

clear_btn = ttk.Button(list_control_frame, text="Clear All", command=on_clear_frequencies)
clear_btn.pack(side=tk.LEFT, padx=5)

select_all_btn = ttk.Button(list_control_frame, text="Select All", 
                            command=lambda: freq_listbox.select_set(0, tk.END))
select_all_btn.pack(side=tk.LEFT, padx=5)

# === Tab 3: Monitoring ===
monitor_tab = ttk.Frame(notebook)
notebook.add(monitor_tab, text="IMSI Monitoring")

# Monitor control
monitor_frame = ttk.LabelFrame(monitor_tab, text="Monitor Control", padding=10)
monitor_frame.pack(fill=tk.X, padx=10, pady=10)

# Show current target in monitoring tab
monitor_target_label = ttk.Label(monitor_frame, textvariable=current_target_var, font=('Arial', 10, 'bold'))
monitor_target_label.pack(pady=5)

# Instructions
instructions_label = ttk.Label(monitor_frame, 
                              text="Select frequencies from 'Frequency Management' tab, then start monitoring",
                              foreground="gray")
instructions_label.pack(pady=5)

monitor_start_btn = ttk.Button(monitor_frame, text="Start Monitoring", 
                               command=on_start_monitoring)
monitor_start_btn.pack(pady=5)

monitor_status_label = ttk.Label(monitor_frame, text="Ready", foreground="green")
monitor_status_label.pack(pady=5)

# Active monitors display
active_frame = ttk.LabelFrame(monitor_tab, text="Active Monitors", padding=10)
active_frame.pack(fill=tk.X, padx=10, pady=10)

active_text = scrolledtext.ScrolledText(active_frame, height=5, width=80)
active_text.pack(fill=tk.X)

# Results display
results_frame = ttk.LabelFrame(monitor_tab, text="Detection Results", padding=10)
results_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

results_listbox = tk.Listbox(results_frame, height=10)
results_listbox.pack(fill=tk.BOTH, expand=True)

# === Tab 4: Logs ===
log_tab = ttk.Frame(notebook)
notebook.add(log_tab, text="Logs")

log_frame = ttk.LabelFrame(log_tab, text="System Logs", padding=10)
log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

log_box = scrolledtext.ScrolledText(
    log_frame,
    width=100,
    height=25,
    state='disabled',
    bg='black',
    fg='lightgreen',
    font=('Consolas', 9)
)
log_box.pack(fill=tk.BOTH, expand=True)

# Start async loop in background thread
logger.info("[INIT] Starting async event loop...")
async_thread = threading.Thread(target=controller.start_async_loop, daemon=True)
async_thread.start()
time.sleep(0.5)  # Give the loop time to start

# Setup cleanup handler
root.protocol("WM_DELETE_WINDOW", on_closing)
root.after(100, update_log_display)

logger.info("[GUI] Async IMSI Scanner ready")
root.mainloop()
