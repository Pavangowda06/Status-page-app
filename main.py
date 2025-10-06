from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
import json
import os
from typing import List, Dict, Any, Optional
import asyncio
from dotenv import load_dotenv
import google.generativeai as genai
import re
from contextlib import asynccontextmanager
import hashlib
import logging
import sys
from fastapi.staticfiles import StaticFiles
try:
    import fcntl
    WINDOWS = False
except ImportError:
    import msvcrt
    WINDOWS = True
import time

# Load environment variables
load_dotenv()

# Enhanced logging setup
class UnicodeLoggingHandler(logging.StreamHandler):
    """Custom handler to properly handle Unicode characters on Windows"""
    def __init__(self):
        super().__init__(sys.stdout)
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('status_monitor.log', encoding='utf-8'),
        UnicodeLoggingHandler()
    ]
)
logger = logging.getLogger(__name__)

# Global variables for enhanced status tracking
previous_status = {}
monitoring_active = False
monitoring_task = None
notification_history = []
rate_limiter = {}
last_notification_times = {}
status_change_buffer = {}
instance_id = f"monitor_{int(time.time())}_{os.getpid()}"
lock_file_path = "status_monitor.lock"

def acquire_global_lock():
    """Acquire global lock to ensure only one monitoring instance"""
    try:
        if WINDOWS:
            # Windows file locking
            lock_file = open(lock_file_path, 'w')
            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                lock_file.write(f"{instance_id}\n{datetime.utcnow().isoformat()}")
                lock_file.flush()
                logger.info(f"Global lock acquired by instance {instance_id}")
                return lock_file
            except OSError:
                lock_file.close()
                logger.info(f"Global lock already held by another instance")
                return None
        else:
            # Unix file locking
            lock_file = open(lock_file_path, 'w')
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock_file.write(f"{instance_id}\n{datetime.utcnow().isoformat()}")
            lock_file.flush()
            logger.info(f"Global lock acquired by instance {instance_id}")
            return lock_file
    except (IOError, OSError):
        logger.info(f"Global lock already held by another instance")
        return None

def release_global_lock(lock_file):
    """Release global lock"""
    if lock_file:
        try:
            if WINDOWS:
                # Windows file unlocking
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                # Unix file unlocking
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()
            if os.path.exists(lock_file_path):
                os.remove(lock_file_path)
            logger.info(f"Global lock released by instance {instance_id}")
        except:
            pass

# Global lock file handle
global_lock = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Enhanced lifespan manager with global lock management"""
    global monitoring_task, global_lock
    
    # Try to acquire global lock
    global_lock = acquire_global_lock()
    
    if global_lock:
        logger.info("Starting primary monitoring instance")
        try:
            monitoring_task = asyncio.create_task(enhanced_continuous_monitoring())
            logger.info("Monitoring task started successfully")
        except Exception as e:
            logger.error(f"Failed to start monitoring: {e}")
    else:
        logger.info("Secondary instance detected - monitoring disabled")
    
    yield
    
    if monitoring_task:
        monitoring_task.cancel()
        try:
            await monitoring_task
        except asyncio.CancelledError:
            logger.info("Monitoring task cancelled cleanly")
    
    await save_monitoring_state()
    release_global_lock(global_lock)
    logger.info("Enhanced monitoring system stopped")

app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Enhanced Slack Configuration with better defaults
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL", "#status-alerts")
SLACK_USERNAME = os.getenv("SLACK_USERNAME", "StatusBot")
SLACK_ICON_EMOJI = os.getenv("SLACK_ICON_EMOJI", ":warning:")
MONITORING_INTERVAL = int(os.getenv("MONITORING_INTERVAL", "180"))  # 3 minutes for stability
NOTIFICATION_COOLDOWN = int(os.getenv("NOTIFICATION_COOLDOWN", "600"))  # 10 minutes cooldown
MAX_NOTIFICATIONS_PER_HOUR = int(os.getenv("MAX_NOTIFICATIONS_PER_HOUR", "10"))
CHANGE_CONFIRMATION_CYCLES = int(os.getenv("CHANGE_CONFIRMATION_CYCLES", "2"))  # Require 2 confirmations

# Enhanced service monitoring configuration
MONITORED_SERVICES = [
    "github", "datadog", "jira", "jsm", "prisma", "grafana", "okta", "cleverbridge", "azure", "aws"
]

SERVICE_PRIORITIES = {
    "github": "high",
    "datadog": "critical", 
    "jira": "medium",
    "jsm": "high",
    "prisma": "high",
    "grafana": "medium",
    "okta": "critical",
    "cleverbridge": "low",
    "azure": "critical",
    "aws": "critical"
}

# Enhanced Google Gemini Configuration
gemini_client = None
try:
    api_key = os.getenv("GEMINI_API_KEY")
    if api_key:
        genai.configure(api_key=api_key)
        gemini_client = genai.GenerativeModel('gemini-2.5-flash')
        logger.info(f"Gemini client initialized")
    else:
        logger.warning("GEMINI_API_KEY not found in environment variables")
except Exception as e:
    logger.error(f"Error initializing Gemini client: {e}")

# Service configurations
datadog_regions = {
    "EU": "https://status.datadoghq.eu",
    "US1": "https://status.datadoghq.com", 
    "US3": "https://status.us3.datadoghq.com",
    "US5": "https://status.us5.datadoghq.com",
    "AP1": "https://status.ap1.datadoghq.com",
    "GovCloud": "https://status.ddog-gov.com",
    "AP2": "https://status.ap2.datadoghq.com"
}

github_components_to_show = [
    "Git Operations", "Webhooks", "API Requests", "Issues", "Pull Requests",
    "Actions", "Packages", "Pages", "Codespaces", "Copilot", "GitHub Mobile"
]

# Enhanced Pydantic models
class ChatMessage(BaseModel):
    message: str

class ChatResponse(BaseModel):
    response: str
    sources: List[str] = []

class EnhancedSlackNotification(BaseModel):
    service: str
    previous_status: str
    current_status: str
    timestamp: str
    severity: str = "info"
    priority: str = "medium"
    components_affected: List[str] = []
    duration: str = "unknown"
    impact_description: str = ""
    recovery_eta: Optional[str] = None
    incident_url: Optional[str] = None
    region: Optional[str] = None
    incident_id: Optional[str] = None

def create_notification_hash(notification: EnhancedSlackNotification) -> str:
    """Create unique hash for notification to prevent duplicates"""
    content = f"{notification.service}_{notification.previous_status}_{notification.current_status}_{notification.timestamp[:16]}"
    return hashlib.md5(content.encode()).hexdigest()

async def fetch_detailed_incident_data():
    """Fetch detailed incident data from official APIs for chatbot"""
    detailed_data = {}
    
    timeout = httpx.Timeout(30.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        
        # GitHub incidents
        try:
            # Get current incidents
            incidents_res = await client.get("https://www.githubstatus.com/api/v2/incidents.json")
            github_data = {"incidents": [], "components": [], "overall_status": "operational"}
            
            if incidents_res.status_code == 200:
                incidents_data = incidents_res.json()
                
                # Get active/recent incidents
                for incident in incidents_data.get("incidents", [])[:5]:
                    status = incident.get("status", "").lower()
                    if status not in ["resolved", "postmortem"]:
                        incident_info = {
                            "name": incident.get("name", "Unknown incident"),
                            "status": incident.get("status", "investigating"),
                            "impact": incident.get("impact", "none"),
                            "created_at": incident.get("created_at", ""),
                            "updated_at": incident.get("updated_at", ""),
                            "monitoring_at": incident.get("monitoring_at", ""),
                            "resolved_at": incident.get("resolved_at", ""),
                            "components": [comp.get("name", "") for comp in incident.get("components", [])],
                            "updates": []
                        }
                        
                        # Get latest updates
                        for update in incident.get("incident_updates", [])[:3]:
                            incident_info["updates"].append({
                                "body": update.get("body", ""),
                                "status": update.get("status", ""),
                                "created_at": update.get("created_at", "")
                            })
                        
                        github_data["incidents"].append(incident_info)
                
                if not github_data["incidents"]:
                    github_data["overall_status"] = "operational"
                else:
                    github_data["overall_status"] = "incident"
            
            # Get component status
            comp_res = await client.get("https://www.githubstatus.com/api/v2/components.json")
            if comp_res.status_code == 200:
                comp_data = comp_res.json()
                for comp in comp_data.get("components", []):
                    if comp.get("name") in github_components_to_show:
                        github_data["components"].append({
                            "name": comp.get("name", ""),
                            "status": comp.get("status", "operational"),
                            "description": comp.get("description", "")
                        })
            
            detailed_data["github"] = github_data
            
        except Exception as e:
            logger.error(f"Error fetching GitHub incident data: {e}")
            detailed_data["github"] = {"incidents": [], "error": str(e)}

        # Datadog incidents
        try:
            datadog_data = {"regions": {}, "overall_status": "operational"}
            
            for region, url in datadog_regions.items():
                try:
                    status_res = await client.get(f"{url}/api/v2/status.json")
                    incidents_res = await client.get(f"{url}/api/v2/incidents.json")
                    
                    region_data = {"status": "operational", "incidents": []}
                    
                    if status_res.status_code == 200:
                        status_data = status_res.json()
                        indicator = status_data.get("status", {}).get("indicator", "none")
                        description = status_data.get("status", {}).get("description", "")
                        
                        region_data["status"] = indicator
                        region_data["description"] = description
                        
                        if indicator != "none":
                            datadog_data["overall_status"] = "incident"
                    
                    if incidents_res.status_code == 200:
                        incidents_data = incidents_res.json()
                        
                        for incident in incidents_data.get("incidents", [])[:3]:
                            if incident.get("status", "").lower() not in ["resolved", "postmortem"]:
                                incident_info = {
                                    "name": incident.get("name", "Unknown incident"),
                                    "status": incident.get("status", "investigating"),
                                    "impact": incident.get("impact", "none"),
                                    "created_at": incident.get("created_at", ""),
                                    "updated_at": incident.get("updated_at", ""),
                                    "monitoring_at": incident.get("monitoring_at", ""),
                                    "resolved_at": incident.get("resolved_at", ""),
                                    "components": [comp.get("name", "") for comp in incident.get("components", [])],
                                    "updates": []
                                }
                                
                                # Get latest updates
                                for update in incident.get("incident_updates", [])[:2]:
                                    incident_info["updates"].append({
                                        "body": update.get("body", ""),
                                        "status": update.get("status", ""),
                                        "created_at": update.get("created_at", "")
                                    })
                                
                                region_data["incidents"].append(incident_info)
                                datadog_data["overall_status"] = "incident"
                    
                    datadog_data["regions"][region] = region_data
                    
                except Exception as e:
                    logger.error(f"Error fetching Datadog {region} data: {e}")
                    datadog_data["regions"][region] = {"status": "error", "error": str(e)}
            
            detailed_data["datadog"] = datadog_data
            
        except Exception as e:
            logger.error(f"Error fetching Datadog incident data: {e}")
            detailed_data["datadog"] = {"regions": {}, "error": str(e)}

        # Jira incidents
        try:
            incidents_res = await client.get("https://jira-software.status.atlassian.com/api/v2/incidents.json")
            jira_data = {"incidents": [], "components": [], "overall_status": "operational"}
            
            if incidents_res.status_code == 200:
                incidents_data = incidents_res.json()
                
                for incident in incidents_data.get("incidents", [])[:5]:
                    if incident.get("status", "").lower() not in ["resolved", "postmortem"]:
                        incident_info = {
                            "name": incident.get("name", "Unknown incident"),
                            "status": incident.get("status", "investigating"),
                            "impact": incident.get("impact", "none"),
                            "created_at": incident.get("created_at", ""),
                            "updated_at": incident.get("updated_at", ""),
                            "monitoring_at": incident.get("monitoring_at", ""),
                            "resolved_at": incident.get("resolved_at", ""),
                            "components": [comp.get("name", "") for comp in incident.get("components", [])],
                            "updates": []
                        }
                        
                        for update in incident.get("incident_updates", [])[:2]:
                            incident_info["updates"].append({
                                "body": update.get("body", ""),
                                "status": update.get("status", ""),
                                "created_at": update.get("created_at", "")
                            })
                        
                        jira_data["incidents"].append(incident_info)
                        jira_data["overall_status"] = "incident"
                
                if not jira_data["incidents"]:
                    jira_data["overall_status"] = "operational"
            
            detailed_data["jira"] = jira_data
            
        except Exception as e:
            logger.error(f"Error fetching Jira incident data: {e}")
            detailed_data["jira"] = {"incidents": [], "error": str(e)}

        # JSM incidents
        try:
            incidents_res = await client.get("https://jira-service-management.status.atlassian.com/api/v2/incidents.json")
            jsm_data = {"incidents": [], "components": [], "overall_status": "operational"}
            
            if incidents_res.status_code == 200:
                incidents_data = incidents_res.json()
                
                for incident in incidents_data.get("incidents", [])[:5]:
                    if incident.get("status", "").lower() not in ["resolved", "postmortem"]:
                        incident_info = {
                            "name": incident.get("name", "Unknown incident"),
                            "status": incident.get("status", "investigating"),
                            "impact": incident.get("impact", "none"),
                            "created_at": incident.get("created_at", ""),
                            "updated_at": incident.get("updated_at", ""),
                            "monitoring_at": incident.get("monitoring_at", ""),
                            "resolved_at": incident.get("resolved_at", ""),
                            "components": [comp.get("name", "") for comp in incident.get("components", [])],
                            "updates": []
                        }
                        
                        for update in incident.get("incident_updates", [])[:2]:
                            incident_info["updates"].append({
                                "body": update.get("body", ""),
                                "status": update.get("status", ""),
                                "created_at": update.get("created_at", "")
                            })
                        
                        jsm_data["incidents"].append(incident_info)
                        jsm_data["overall_status"] = "incident"
                
                if not jsm_data["incidents"]:
                    jsm_data["overall_status"] = "operational"
            
            detailed_data["jsm"] = jsm_data
            
        except Exception as e:
            logger.error(f"Error fetching JSM incident data: {e}")
            detailed_data["jsm"] = {"incidents": [], "error": str(e)}

        # Prisma incidents
        try:
            incidents_res = await client.get("https://www.prisma-status.com/api/v2/incidents.json")
            prisma_data = {"incidents": [], "overall_status": "operational"}
            
            if incidents_res.status_code == 200:
                incidents_data = incidents_res.json()
                
                for incident in incidents_data.get("incidents", [])[:5]:
                    if incident.get("status", "").lower() not in ["resolved", "postmortem"]:
                        incident_info = {
                            "name": incident.get("name", "Unknown incident"),
                            "status": incident.get("status", "investigating"),
                            "impact": incident.get("impact", "none"),
                            "created_at": incident.get("created_at", ""),
                            "updated_at": incident.get("updated_at", ""),
                            "monitoring_at": incident.get("monitoring_at", ""),
                            "resolved_at": incident.get("resolved_at", ""),
                            "components": [comp.get("name", "") for comp in incident.get("components", [])],
                            "updates": []
                        }
                        
                        for update in incident.get("incident_updates", [])[:2]:
                            incident_info["updates"].append({
                                "body": update.get("body", ""),
                                "status": update.get("status", ""),
                                "created_at": update.get("created_at", "")
                            })
                        
                        prisma_data["incidents"].append(incident_info)
                        prisma_data["overall_status"] = "incident"
                
                if not prisma_data["incidents"]:
                    prisma_data["overall_status"] = "operational"
            
            detailed_data["prisma"] = prisma_data
            
        except Exception as e:
            logger.error(f"Error fetching Prisma incident data: {e}")
            detailed_data["prisma"] = {"incidents": [], "error": str(e)}

        # Grafana incidents
        try:
            incidents_res = await client.get("https://status.grafana.com/api/v2/incidents.json")
            grafana_data = {"incidents": [], "overall_status": "operational"}
            
            if incidents_res.status_code == 200:
                incidents_data = incidents_res.json()
                
                for incident in incidents_data.get("incidents", [])[:5]:
                    if incident.get("status", "").lower() not in ["resolved", "postmortem"]:
                        incident_info = {
                            "name": incident.get("name", "Unknown incident"),
                            "status": incident.get("status", "investigating"),
                            "impact": incident.get("impact", "none"),
                            "created_at": incident.get("created_at", ""),
                            "updated_at": incident.get("updated_at", ""),
                            "monitoring_at": incident.get("monitoring_at", ""),
                            "resolved_at": incident.get("resolved_at", ""),
                            "components": [comp.get("name", "") for comp in incident.get("components", [])],
                            "updates": []
                        }
                        
                        for update in incident.get("incident_updates", [])[:2]:
                            incident_info["updates"].append({
                                "body": update.get("body", ""),
                                "status": update.get("status", ""),
                                "created_at": update.get("created_at", "")
                            })
                        
                        grafana_data["incidents"].append(incident_info)
                        grafana_data["overall_status"] = "incident"
                
                if not grafana_data["incidents"]:
                    grafana_data["overall_status"] = "operational"
            
            detailed_data["grafana"] = grafana_data
            
        except Exception as e:
            logger.error(f"Error fetching Grafana incident data: {e}")
            detailed_data["grafana"] = {"incidents": [], "error": str(e)}

        # Okta incidents (RSS feed parsing)
        try:
            okta_res = await client.get("https://feeds.feedburner.com/OktaTrustRSS")
            okta_data = {"incidents": [], "overall_status": "operational"}
            
            if okta_res.status_code == 200:
                root = ET.fromstring(okta_res.text)
                channel = root.find("channel")
                items = channel.findall("item") if channel is not None else []
                
                recent_cutoff = datetime.utcnow() - timedelta(hours=48)
                
                for item in items[:10]:
                    title = item.find("title").text if item.find("title") is not None else "Unknown"
                    description = item.find("description").text if item.find("description") is not None else ""
                    pub_date = item.find("pubDate").text if item.find("pubDate") is not None else ""
                    link = item.find("link").text if item.find("link") is not None else ""
                    
                    try:
                        if pub_date:
                            pub_datetime = datetime.strptime(pub_date, '%a, %d %b %Y %H:%M:%S %z').replace(tzinfo=None)
                        else:
                            pub_datetime = datetime.utcnow()
                    except:
                        pub_datetime = datetime.utcnow()
                    
                    # Only include recent, unresolved incidents
                    if (pub_datetime > recent_cutoff and 
                        "resolved" not in title.lower() and 
                        "operational" not in title.lower()):
                        
                        incident_info = {
                            "name": title,
                            "description": description[:500] + "..." if len(description) > 500 else description,
                            "status": "investigating" if "investigating" in title.lower() else "incident",
                            "published": pub_date,
                            "link": link,
                            "age_hours": int((datetime.utcnow() - pub_datetime).total_seconds() / 3600)
                        }
                        
                        okta_data["incidents"].append(incident_info)
                        okta_data["overall_status"] = "incident"
                
                if not okta_data["incidents"]:
                    okta_data["overall_status"] = "operational"
            
            detailed_data["okta"] = okta_data
            
        except Exception as e:
            logger.error(f"Error fetching Okta incident data: {e}")
            detailed_data["okta"] = {"incidents": [], "error": str(e)}

        # AWS data from JSON file
        try:
            aws_raw = load_aws_status()
            aws_processed, aws_color, aws_status = process_aws_data(aws_raw)
            
            aws_data = {
                "overall_status": aws_status.lower().replace(" ", "_"),
                "status_color": aws_color,
                "regions_summary": {}
            }
            
            # Extract affected regions/services
            affected_regions = []
            for geography, geo_data in aws_processed.items():
                if isinstance(geo_data, dict) and "regions" in geo_data:
                    for region, region_data in geo_data["regions"].items():
                        if isinstance(region_data, dict) and "_region_stats" in region_data:
                            stats = region_data["_region_stats"]
                            if stats.get("status_color") in ["red", "orange"]:
                                affected_count = stats.get("total_service_count", 0) - stats.get("green_service_count", 0)
                                if affected_count > 0:
                                    affected_regions.append({
                                        "geography": geography,
                                        "region": region,
                                        "affected_services": affected_count,
                                        "total_services": stats.get("total_service_count", 0),
                                        "status_color": stats.get("status_color")
                                    })
            
            aws_data["affected_regions"] = affected_regions[:10]  # Limit to top 10
            aws_data["total_affected_regions"] = len(affected_regions)
            
            detailed_data["aws"] = aws_data
            logger.info(f"AWS data loaded: {aws_status}, {len(affected_regions)} affected regions")
            
        except Exception as e:
            logger.error(f"Error fetching AWS data: {e}")
            detailed_data["aws"] = {"overall_status": "error", "error": str(e)}

        # Azure data from JSON file
        try:
            azure_raw = load_azure_status()
            azure_processed, azure_color, azure_status = process_azure_data(azure_raw)
            
            azure_data = {
                "overall_status": azure_status.lower().replace(" ", "_"),
                "status_color": azure_color,
                "regions_summary": {}
            }
            
            # Extract affected regions/services
            affected_regions = []
            for geography, geo_data in azure_processed.items():
                if isinstance(geo_data, dict) and "regions" in geo_data:
                    for region, region_data in geo_data["regions"].items():
                        if isinstance(region_data, dict) and "_region_stats" in region_data:
                            stats = region_data["_region_stats"]
                            if stats.get("status_color") in ["red", "orange"]:
                                affected_count = stats.get("total_service_count", 0) - stats.get("green_service_count", 0)
                                if affected_count > 0:
                                    affected_regions.append({
                                        "geography": geography,
                                        "region": region,
                                        "affected_services": affected_count,
                                        "total_services": stats.get("total_service_count", 0),
                                        "status_color": stats.get("status_color")
                                    })
            
            azure_data["affected_regions"] = affected_regions[:10]  # Limit to top 10
            azure_data["total_affected_regions"] = len(affected_regions)
            
            detailed_data["azure"] = azure_data
            logger.info(f"Azure data loaded: {azure_status}, {len(affected_regions)} affected regions")
            
        except Exception as e:
            logger.error(f"Error fetching Azure data: {e}")
            detailed_data["azure"] = {"overall_status": "error", "error": str(e)}

        # Cleverbridge incidents (RSS feed parsing)
        try:
            cb_res = await client.get("https://status.cleverbridge.com/history.rss")
            cb_data = {"incidents": [], "overall_status": "operational"}
            
            if cb_res.status_code == 200:
                root = ET.fromstring(cb_res.text)
                channel = root.find("channel")
                items = channel.findall("item") if channel is not None else []
                
                recent_cutoff = datetime.utcnow() - timedelta(hours=48)
                
                for item in items[:10]:
                    title = item.find("title").text if item.find("title") is not None else "Unknown"
                    description = item.find("description").text if item.find("description") is not None else ""
                    pub_date = item.find("pubDate").text if item.find("pubDate") is not None else ""
                    link = item.find("link").text if item.find("link") is not None else ""
                    
                    try:
                        if pub_date:
                            pub_datetime = datetime.strptime(pub_date, '%a, %d %b %Y %H:%M:%S %z').replace(tzinfo=None)
                        else:
                            pub_datetime = datetime.utcnow()
                    except:
                        pub_datetime = datetime.utcnow()
                    
                    if (pub_datetime > recent_cutoff and 
                        "resolved" not in title.lower() and
                        "operational" not in title.lower()):
                        
                        incident_info = {
                            "name": title,
                            "description": description[:500] + "..." if len(description) > 500 else description,
                            "status": "investigating" if "investigating" in title.lower() else "incident",
                            "published": pub_date,
                            "link": link,
                            "age_hours": int((datetime.utcnow() - pub_datetime).total_seconds() / 3600)
                        }
                        
                        cb_data["incidents"].append(incident_info)
                        cb_data["overall_status"] = "incident"
                
                if not cb_data["incidents"]:
                    cb_data["overall_status"] = "operational"
            
            detailed_data["cleverbridge"] = cb_data
            
        except Exception as e:
            logger.error(f"Error fetching Cleverbridge incident data: {e}")
            detailed_data["cleverbridge"] = {"incidents": [], "error": str(e)}

    return detailed_data

def normalize_status(status: str) -> str:
    """Improved status normalization"""
    if not status:
        return "unknown"
    
    status_lower = str(status).lower().strip()
    
    # Normalize common variations
    if status_lower in ["operational", "available", "normal", "ok", "green", "up"]:
        return "operational"
    elif status_lower in ["degraded performance", "degraded_performance", "degraded", "partial_outage", "partial outage", "minor issue", "minor_issue"]:
        return "degraded"
    elif status_lower in ["major_outage", "major outage", "down", "red", "critical", "outage", "error"]:
        return "major_outage"
    elif status_lower in ["maintenance", "scheduled maintenance", "under_maintenance"]:
        return "maintenance"
    elif status_lower in ["investigating", "identified", "monitoring"]:
        return "investigating"
    else:
        return status_lower

def get_status_emoji(status: str, severity: str = "info") -> str:
    """Get appropriate emoji for status"""
    normalized = normalize_status(status)
    
    if normalized == "operational":
        return ":white_check_mark:"
    elif normalized in ["degraded", "investigating"]:
        return ":warning:"
    elif normalized == "major_outage":
        return ":red_circle:"
    elif normalized == "maintenance":
        return ":wrench:"
    else:
        return ":question:"

def get_priority_color(priority: str, status_change: str) -> str:
    """Enhanced color determination"""
    normalized_current = normalize_status(status_change)
    
    if normalized_current == "operational":
        return "good"
    elif normalized_current == "major_outage":
        return "danger"
    elif normalized_current in ["degraded", "investigating"]:
        return "warning"
    elif normalized_current == "maintenance":
        return "#439FE0"
    else:
        priority_colors = {
            "critical": "danger",
            "high": "warning", 
            "medium": "warning",
            "low": "#439FE0"
        }
        return priority_colors.get(priority, "warning")

def should_send_notification(notification: EnhancedSlackNotification) -> bool:
    """Enhanced rate limiting with deduplication"""
    global rate_limiter, notification_history, last_notification_times
    
    current_time = datetime.utcnow()
    service = notification.service
    
    # Create notification hash for deduplication
    notification_hash = create_notification_hash(notification)
    
    # Check for duplicate notifications in recent history
    for entry in notification_history[-20:]:  # Check last 20 notifications
        if entry.get('hash') == notification_hash:
            time_diff = (current_time - entry.get('timestamp', current_time)).total_seconds()
            if time_diff < 1800:  # 30 minutes
                logger.info(f"Duplicate notification blocked for {service} (hash: {notification_hash[:8]})")
                return False
    
    # Always allow operational recovery notifications (but check cooldown)
    if normalize_status(notification.current_status) == "operational" and normalize_status(notification.previous_status) != "operational":
        if service in last_notification_times:
            time_diff = (current_time - last_notification_times[service]).total_seconds()
            if time_diff < 300:  # 5 minute minimum between any notifications for same service
                logger.info(f"Recovery notification blocked - too recent for {service}")
                return False
        
        logger.info(f"Allowing recovery notification for {service}")
        last_notification_times[service] = current_time
        return True
    
    # Enhanced cooldown check for non-recovery notifications
    if service in last_notification_times:
        time_diff = (current_time - last_notification_times[service]).total_seconds()
        if time_diff < NOTIFICATION_COOLDOWN:
            logger.info(f"Notification blocked for {service} - cooldown active ({NOTIFICATION_COOLDOWN - time_diff:.0f}s remaining)")
            return False
    
    # Check hourly rate limit
    recent_notifications = [
        entry for entry in notification_history 
        if current_time - entry.get('timestamp', current_time) < timedelta(hours=1)
    ]
    
    if len(recent_notifications) >= MAX_NOTIFICATIONS_PER_HOUR:
        logger.warning(f"Rate limit exceeded, skipping notification for {notification.service}")
        return False
    
    # Add to history and update last notification time
    notification_history.append({
        'service': notification.service,
        'timestamp': current_time,
        'status_change': f"{notification.previous_status} -> {notification.current_status}",
        'hash': notification_hash
    })
    
    last_notification_times[service] = current_time
    return True

def format_component_list(components: List[str], service: str) -> str:
    """Format affected components list for notification"""
    if not components:
        return ""
    
    if len(components) == 1:
        return f"• Component: {components[0]}"
    elif len(components) <= 3:
        return f"• Components: {', '.join(components)}"
    else:
        return f"• Components: {', '.join(components[:3])} and {len(components) - 3} more"

def create_enhanced_slack_message(notification: EnhancedSlackNotification) -> dict:
    """Create enhanced Slack message with proper formatting"""
    status_emoji = get_status_emoji(notification.current_status, notification.severity)
    color = get_priority_color(notification.priority, notification.current_status)
    
    # Enhanced title and message formatting
    normalized_current = normalize_status(notification.current_status)
    normalized_previous = normalize_status(notification.previous_status)
    
    # Determine change type and create appropriate message
    if normalized_current == "operational" and normalized_previous != "operational":
        # Recovery notification
        title = f"{status_emoji} Service Recovered"
        message_text = f"*{notification.service.upper()}* has returned to operational status"
        if notification.components_affected:
            component_text = format_component_list(notification.components_affected, notification.service)
            message_text += f"\n{component_text}"
    elif normalized_current == "maintenance":
        # Maintenance notification
        title = f":wrench: Maintenance Started"
        message_text = f"*{notification.service.upper()}* is undergoing scheduled maintenance"
    else:
        # Issue notification
        if normalized_current == "major_outage":
            severity_indicator = ":rotating_light: CRITICAL: "
        elif notification.priority == "critical":
            severity_indicator = ":rotating_light: "
        else:
            severity_indicator = ""
        
        title = f"{status_emoji} {severity_indicator}Service Issue Detected"
        message_text = f"*{notification.service.upper()}* status changed from *{notification.previous_status}* to *{notification.current_status}*"
        
        if notification.components_affected:
            component_text = format_component_list(notification.components_affected, notification.service)
            message_text += f"\n{component_text}"
    
    # Create fields
    fields = [
        {
            "title": "Service",
            "value": f"*{notification.service.upper()}*",
            "short": True
        },
        {
            "title": "Status Change",
            "value": f"{notification.previous_status} → *{notification.current_status}*",
            "short": True
        },
        {
            "title": "Priority",
            "value": f"*{notification.priority.title()}*",
            "short": True
        },
        {
            "title": "Detected",
            "value": notification.timestamp,
            "short": True
        }
    ]
    
    # Add incident URL if available
    if notification.incident_url:
        fields.append({
            "title": "Status Page",
            "value": f"<{notification.incident_url}|View Details>",
            "short": False
        })
    
    timestamp_unix = int(datetime.utcnow().timestamp())
    
    payload = {
        "text": f"Status Alert: {notification.service.upper()}",
        "attachments": [{
            "fallback": f"{notification.service.upper()}: {notification.previous_status} → {notification.current_status}",
            "color": color,
            "title": title,
            "text": message_text,
            "fields": fields,
            "footer": "Status Monitor",
            "ts": timestamp_unix
        }]
    }
    
    return payload

async def send_enhanced_slack_notification(notification: EnhancedSlackNotification) -> bool:
    """Enhanced Slack notification with deduplication and proper formatting"""
    if not SLACK_WEBHOOK_URL:
        logger.warning("Slack webhook URL not configured")
        return False
    
    if not should_send_notification(notification):
        return False
    
    try:
        payload = create_enhanced_slack_message(notification)
        
        logger.info(f"Sending Slack notification for {notification.service}: {notification.previous_status} → {notification.current_status}")
        
        timeout = httpx.Timeout(30.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                SLACK_WEBHOOK_URL,
                json=payload,
                headers={"Content-Type": "application/json"}
            )
            
            if response.status_code == 200:
                logger.info(f"Slack notification sent successfully for {notification.service}")
                return True
            else:
                logger.error(f"Slack notification failed: {response.status_code} - {response.text}")
                return False
                        
    except Exception as e:
        logger.error(f"Critical error in Slack notification for {notification.service}: {e}")
        return False

def detect_enhanced_status_changes(current_status: dict, previous: dict) -> List[EnhancedSlackNotification]:
    """Enhanced change detection with better confirmation logic"""
    global status_change_buffer
    
    notifications = []
    current_time = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    
    logger.info("Starting status change detection...")
    
    for service in MONITORED_SERVICES:
        if service not in current_status.get("details", {}):
            logger.debug(f"Service {service} not found in current status")
            continue
            
        current_service_status = current_status["details"][service]
        previous_service_status = previous.get("details", {}).get(service, "unknown")
        
        # Normalize statuses for comparison
        current_normalized = normalize_status(current_service_status)
        previous_normalized = normalize_status(previous_service_status)
        
        logger.info(f"Checking {service}: '{previous_service_status}' ({previous_normalized}) → '{current_service_status}' ({current_normalized})")
        
        # Skip if no meaningful change
        if current_normalized == previous_normalized and previous_normalized != "unknown":
            logger.debug(f"No change for {service}")
            continue
        
        # Skip initial operational state
        if previous_normalized == "unknown" and current_normalized == "operational":
            logger.debug(f"Skipping initial operational state for {service}")
            continue
        
        # Buffer key for tracking changes
        buffer_key = f"{service}_{current_normalized}"
        
        # For critical changes or recovery, send immediately
        if (current_normalized in ["major_outage"] or 
            (current_normalized == "operational" and previous_normalized in ["major_outage", "degraded"])):
            logger.info(f"Critical/Recovery change detected for {service} - processing immediately")
        else:
            # Use buffer for other changes
            if buffer_key not in status_change_buffer:
                status_change_buffer[buffer_key] = {
                    'count': 1,
                    'first_seen': datetime.utcnow(),
                    'previous_status': previous_service_status,
                    'current_status': current_service_status,
                    'normalized_current': current_normalized,
                    'normalized_previous': previous_normalized
                }
                logger.info(f"Buffering change for {service}: {previous_normalized} → {current_normalized} (1/{CHANGE_CONFIRMATION_CYCLES})")
                
                if CHANGE_CONFIRMATION_CYCLES > 1:
                    continue
            else:
                status_change_buffer[buffer_key]['count'] += 1
                logger.info(f"Confirming change for {service}: {previous_normalized} → {current_normalized} ({status_change_buffer[buffer_key]['count']}/{CHANGE_CONFIRMATION_CYCLES})")
            
            # Only trigger notification after confirmation cycles
            if status_change_buffer[buffer_key]['count'] < CHANGE_CONFIRMATION_CYCLES:
                continue
            
            # Clear buffer entry after processing
            buffer_data = status_change_buffer.pop(buffer_key, {})
        
        # Enhanced component analysis
        affected_components = []
        region_info = None
        
        try:
            if service in current_status.get("components", {}):
                components = current_status["components"][service]
                
                if isinstance(components, dict):
                    for comp_name, comp_info in components.items():
                        if isinstance(comp_info, dict):
                            comp_status = normalize_status(comp_info.get("status", ""))
                            if comp_status != "operational":
                                affected_components.append(comp_name)
                        elif isinstance(comp_info, str):
                            comp_status = normalize_status(comp_info)
                            if comp_status != "operational":
                                affected_components.append(comp_name)
                elif isinstance(components, list):
                    for comp in components:
                        if isinstance(comp, dict):
                            comp_status = normalize_status(comp.get("status", ""))
                            comp_name = comp.get("name", "Unknown Component")
                            if comp_status != "operational":
                                affected_components.append(comp_name)
        except Exception as e:
            logger.error(f"Error analyzing components for {service}: {e}")
        
        # Enhanced severity and priority determination
        priority = SERVICE_PRIORITIES.get(service, "medium")
        
        if current_normalized == "operational":
            severity = "resolved"
        elif current_normalized == "major_outage":
            severity = "critical"
        elif current_normalized in ["degraded", "investigating"]:
            severity = "warning"
        elif current_normalized == "maintenance":
            severity = "info"
        else:
            severity = "warning"
        
        # Generate incident URL
        incident_urls = {
            "github": "https://www.githubstatus.com",
            "datadog": "https://status.datadoghq.com",
            "jira": "https://jira-software.status.atlassian.com",
            "jsm": "https://jira-service-management.status.atlassian.com",
            "prisma": "https://www.prisma-status.com",
            "grafana": "https://status.grafana.com",
            "okta": "https://status.okta.com",
            "cleverbridge": "https://status.cleverbridge.com",
            "azure": "https://status.azure.com",
            "aws": "https://status.aws.amazon.com"
        }
        
        notification = EnhancedSlackNotification(
            service=service,
            previous_status=previous_service_status,
            current_status=current_service_status,
            timestamp=current_time,
            severity=severity,
            priority=priority,
            components_affected=affected_components[:5],  # Limit to 5 components
            duration="ongoing" if current_normalized != "operational" else "resolved",
            incident_url=incident_urls.get(service),
            region=region_info,
            incident_id=f"{service}-{int(datetime.utcnow().timestamp())}"
        )
        
        notifications.append(notification)
        
        logger.info(f"✓ Status change confirmed: {service} {previous_service_status} → {current_service_status} (Priority: {priority}, Severity: {severity})")
    
    logger.info(f"Status change detection complete. Found {len(notifications)} changes.")
    return notifications

# Helper functions for status calculation remain the same
def calculate_status_color(red_issue_count):
    if red_issue_count == 0:
        return "green"
    elif red_issue_count <= 2:
        return "orange"
    else:
        return "red"

def calculate_status_label(red_issue_count):
    if red_issue_count == 0:
        return "OPERATIONAL"
    elif red_issue_count <= 2:
        return "MINOR ISSUE"
    else:
        return "DEGRADED"

# Azure and AWS processing functions remain the same
def load_azure_status():
    try:
        if os.path.exists("azure_status_structured.json"):
            with open("azure_status_structured.json", "r", encoding="utf-8") as f:
                return json.load(f)
        else:
            logger.warning("Azure status file not found")
            return {}
    except Exception as e:
        logger.error(f"Error loading Azure status: {e}")
        return {}

def get_azure_status_color(status_obj):
    if isinstance(status_obj, str):
        status_lower = status_obj.lower()
        if status_lower == "available":
            return "green"
        if status_lower == "n/a":
            return "orange"
    elif isinstance(status_obj, dict):
        return "red"
    return "red"

def process_azure_data(azure_data):
    if not azure_data:
        return {}, "green", "OPERATIONAL"

    processed_data = {}

    for geography, regions in azure_data.items():
        if geography == "Current Impact":
            continue
        processed_data[geography] = {"regions": {}, "_geography_stats": {}}
        geography_red_regions = 0

        for region, groups in regions.items():
            processed_data[geography]["regions"][region] = {}
            region_red_issues = 0
            region_green_services = 0
            total_services_in_region = 0

            if not isinstance(groups, dict):
                continue

            for group_name, services in groups.items():
                if group_name == '_region_stats':
                    continue

                group_services = {}
                if services and isinstance(services, dict):
                    for service_name, status_obj in services.items():
                        total_services_in_region += 1
                        color = get_azure_status_color(status_obj)

                        if color == "red":
                            region_red_issues += 1
                        elif color == "green":
                            region_green_services += 1

                        if isinstance(status_obj, dict):
                            status = status_obj.get("status", "Unknown")
                            severity = status_obj.get("severity", status)
                        else:
                            status = status_obj
                            severity = None

                        group_services[service_name] = {
                            "status": status,
                            "severity": severity,
                            "status_color": color
                        }

                processed_data[geography]["regions"][region][group_name] = group_services

            region_status_color = calculate_status_color(region_red_issues)
            if region_status_color == 'red':
                geography_red_regions += 1

            processed_data[geography]["regions"][region]["_region_stats"] = {
                "status_color": region_status_color,
                "green_service_count": region_green_services,
                "total_service_count": total_services_in_region
            }

        geography_status_color = calculate_status_color(geography_red_regions)
        processed_data[geography]["_geography_stats"] = { "status_color": geography_status_color }

    total_red_geographies = sum(1 for geo in processed_data.values() if geo["_geography_stats"]["status_color"] == 'red')
    azure_color = calculate_status_color(total_red_geographies)
    azure_label = calculate_status_label(total_red_geographies)

    return processed_data, azure_color, azure_label

def load_aws_status():
    try:
        if os.path.exists("aws_services_live.json"):
            with open("aws_services_live.json", "r", encoding="utf-8") as f:
                return json.load(f)
        else:
            logger.warning("AWS status file not found")
            return {}
    except Exception as e:
        logger.error(f"Error loading AWS status: {e}")
        return {}

def get_aws_status_color(status_str):
    if isinstance(status_str, str) and status_str.lower() == "available":
        return "green"
    return "red"

def process_aws_data(aws_data):
    if not aws_data:
        return {}, "green", "OPERATIONAL"

    processed_data = {}
    
    for geography, regions in aws_data.items():
        processed_data[geography] = {"regions": {}, "_geography_stats": {}}
        geography_red_regions = 0

        for region, groups in regions.items():
            processed_data[geography]["regions"][region] = {}
            region_red_issues = 0
            region_green_services = 0
            total_services_in_region = 0

            if not isinstance(groups, dict):
                continue

            for group_name, services in groups.items():
                group_services = {}
                if services and isinstance(services, dict):
                    for service_name, status_str in services.items():
                        total_services_in_region += 1
                        color = get_aws_status_color(status_str)

                        if color == "red":
                            region_red_issues += 1
                        elif color == "green":
                            region_green_services += 1
                        
                        group_services[service_name] = {
                            "status": status_str,
                            "severity": status_str if color != 'green' else None,
                            "status_color": color
                        }
                
                processed_data[geography]["regions"][region][group_name] = group_services
            
            region_status_color = calculate_status_color(region_red_issues)
            if region_status_color == 'red':
                geography_red_regions += 1
            
            processed_data[geography]["regions"][region]["_region_stats"] = {
                "status_color": region_status_color,
                "green_service_count": region_green_services,
                "total_service_count": total_services_in_region
            }

        geography_status_color = calculate_status_color(geography_red_regions)
        processed_data[geography]["_geography_stats"] = { "status_color": geography_status_color }

    total_red_geographies = sum(1 for geo in processed_data.values() if geo["_geography_stats"]["status_color"] == 'red')
    aws_color = calculate_status_color(total_red_geographies)
    aws_label = calculate_status_label(total_red_geographies)
        
    return processed_data, aws_color, aws_label

async def get_current_status_data():
    """Enhanced status data collection with better error handling"""
    details = {}
    status_colors = {}
    components = {}

    logger.info("Starting status data collection...")

    # Azure Processing
    try:
        azure_raw_data = load_azure_status()
        azure_processed, azure_main_color, azure_main_status = process_azure_data(azure_raw_data)
        details["azure"] = azure_main_status
        status_colors["azure"] = azure_main_color
        components["azure"] = azure_processed
        logger.info(f"Azure status: {azure_main_status}")
    except Exception as e:
        logger.error(f"Error processing Azure data: {e}")
        details["azure"] = "ERROR"
        status_colors["azure"] = "red"

    # AWS Processing
    try:
        aws_raw_data = load_aws_status()
        aws_processed, aws_main_color, aws_main_status = process_aws_data(aws_raw_data)
        details["aws"] = aws_main_status
        status_colors["aws"] = aws_main_color
        components["aws"] = aws_processed
        logger.info(f"AWS status: {aws_main_status}")
    except Exception as e:
        logger.error(f"Error processing AWS data: {e}")
        details["aws"] = "ERROR"
        status_colors["aws"] = "red"

    async def fetch_with_retry(client, url, service_name, attempt=0):
        max_retries = 2
        try:
            response = await client.get(url, timeout=25.0)
            response.raise_for_status()
            return response
        except Exception as e:
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt
                logger.warning(f"Retry {attempt + 1} for {service_name} after {wait_time}s: {e}")
                await asyncio.sleep(wait_time)
                return await fetch_with_retry(client, url, service_name, attempt + 1)
            else:
                logger.error(f"Final attempt failed for {service_name}: {e}")
                raise

    timeout = httpx.Timeout(30.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        # GitHub processing
        github_components = {}
        try:
            ghc_res = await fetch_with_retry(client, "https://www.githubstatus.com/api/v2/components.json", "GitHub")
            for comp in ghc_res.json().get("components", []):
                name = comp.get("name")
                status = comp.get("status")
                if name in github_components_to_show:
                    github_components[name] = {
                        "status": status,
                        "severity": status if normalize_status(status) != "operational" else None,
                        "updated_at": comp.get("updated_at")
                    }
            logger.info(f"GitHub components loaded: {len(github_components)}")
        except Exception as e:
            logger.error(f"Error fetching GitHub status: {e}")
            github_components = {"GitHub API": {"status": "error", "severity": "critical"}}

        non_operational = sum(1 for val in github_components.values() if normalize_status(val["status"]) != "operational")
        status_colors["github"] = calculate_status_color(non_operational)
        details["github"] = calculate_status_label(non_operational)
        components["github"] = github_components
        logger.info(f"GitHub status: {details['github']} ({non_operational} non-operational)")

        # Enhanced Datadog processing
        datadog_regions_status = {}
        for region, url in datadog_regions.items():
            try:
                res = await fetch_with_retry(client, f"{url}/api/v2/status.json", f"Datadog {region}")
                data = res.json()
                indicator = data.get("status", {}).get("indicator", "unknown")
                
                # Map Datadog indicators to our status system
                if indicator == "none":
                    status = "operational"
                elif indicator == "minor":
                    status = "minor issue"
                elif indicator == "major":
                    status = "major outage"
                elif indicator == "critical":
                    status = "major outage"
                else:
                    status = indicator
                
                description = data.get("status", {}).get("description", status)
                
                datadog_regions_status[region] = {
                    "status": status,
                    "severity": indicator if status != "operational" else None,
                    "description": description,
                    "updated_at": datetime.utcnow().isoformat()
                }
            except Exception as e:
                logger.error(f"Error fetching Datadog status for {region}: {e}")
                datadog_regions_status[region] = {
                    "status": "error",
                    "severity": "critical",
                    "description": f"Failed to fetch: {str(e)[:50]}"
                }

        non_operational_dd = sum(1 for val in datadog_regions_status.values() if normalize_status(val["status"]) != "operational")
        status_colors["datadog"] = calculate_status_color(non_operational_dd)
        details["datadog"] = calculate_status_label(non_operational_dd)
        components["datadog"] = datadog_regions_status
        logger.info(f"Datadog status: {details['datadog']} ({non_operational_dd} regions non-operational)")

        # Enhanced Jira processing
        jira_components = {}
        try:
            jira_res = await fetch_with_retry(client, "https://jira-software.status.atlassian.com/api/v2/components.json", "Jira")
            for comp in jira_res.json().get("components", []):
                name = comp.get("name")
                status = comp.get("status")
                jira_components[name] = {
                    "status": status,
                    "severity": status if normalize_status(status) != "operational" else None,
                    "updated_at": comp.get("updated_at")
                }
            logger.info(f"Jira components loaded: {len(jira_components)}")
        except Exception as e:
            logger.error(f"Error fetching Jira status: {e}")
            jira_components = {"Jira API": {"status": "error", "severity": "critical"}}

        non_operational_jira = sum(1 for val in jira_components.values() if normalize_status(val["status"]) != "operational")
        status_colors["jira"] = calculate_status_color(non_operational_jira)
        details["jira"] = calculate_status_label(non_operational_jira)
        components["jira"] = jira_components
        logger.info(f"Jira status: {details['jira']} ({non_operational_jira} components non-operational)")

        # Enhanced JSM processing
        jsm_components = {}
        try:
            jsm_res = await fetch_with_retry(client, "https://jira-service-management.status.atlassian.com/api/v2/components.json", "JSM")
            for comp in jsm_res.json().get("components", []):
                name = comp.get("name")
                status = comp.get("status")
                jsm_components[name] = {
                    "status": status,
                    "severity": status if normalize_status(status) != "operational" else None,
                    "updated_at": comp.get("updated_at")
                }
            logger.info(f"JSM components loaded: {len(jsm_components)}")
        except Exception as e:
            logger.error(f"Error fetching JSM status: {e}")
            jsm_components = {"JSM API": {"status": "error", "severity": "critical"}}

        non_operational_jsm = sum(1 for val in jsm_components.values() if normalize_status(val["status"]) != "operational")
        status_colors["jsm"] = calculate_status_color(non_operational_jsm)
        details["jsm"] = calculate_status_label(non_operational_jsm)
        components["jsm"] = jsm_components
        logger.info(f"JSM status: {details['jsm']} ({non_operational_jsm} components non-operational)")

        # Enhanced Prisma processing
        prisma_components = {}
        try:
            prisma_res = await fetch_with_retry(client, "https://www.prisma-status.com/api/v2/components.json", "Prisma")
            for comp in prisma_res.json().get("components", []):
                name = comp.get("name")
                status = comp.get("status")
                prisma_components[name] = {
                    "status": status,
                    "severity": status if normalize_status(status) != "operational" else None,
                    "updated_at": comp.get("updated_at")
                }
            logger.info(f"Prisma components loaded: {len(prisma_components)}")
        except Exception as e:
            logger.error(f"Error fetching Prisma status: {e}")
            prisma_components = {"Prisma API": {"status": "error", "severity": "critical"}}

        non_operational_prisma = sum(1 for val in prisma_components.values() if normalize_status(val["status"]) != "operational")
        status_colors["prisma"] = calculate_status_color(non_operational_prisma)
        details["prisma"] = calculate_status_label(non_operational_prisma)
        components["prisma"] = prisma_components
        logger.info(f"Prisma status: {details['prisma']} ({non_operational_prisma} components non-operational)")

        # Enhanced Grafana processing (FIXED to return a List)
        grafana_components = []  # CHANGED: Back to a list
        try:
            grafana_res = await fetch_with_retry(client, "https://status.grafana.com/api/v2/components.json", "Grafana")
            for comp in grafana_res.json().get("components", []):
                name = comp.get("name")
                status = comp.get("status")
                
                # CHANGED: Appending a dictionary to the list
                grafana_components.append({
                    "name": name,
                    "status": status,
                    "severity": status if normalize_status(status) != "operational" else None,
                    "updated_at": comp.get("updated_at"),
                    "url": f"https://status.grafana.com/components/{comp.get('id')}"
                })
            logger.info(f"Grafana components loaded: {len(grafana_components)}")
            
        except Exception as e:
            logger.error(f"Error fetching Grafana components: {e}")
            # CHANGED: Error case must also be a list
            grafana_components = [{"name": "Grafana API", "status": "error", "severity": "critical"}]

        # CHANGED: Logic updated to count non-operational components in a LIST
        non_operational_grafana = sum(1 for c in grafana_components if normalize_status(c["status"]) != "operational")
        status_colors["grafana"] = calculate_status_color(non_operational_grafana)
        details["grafana"] = calculate_status_label(non_operational_grafana)
        components["grafana"] = grafana_components
        logger.info(f"Grafana status: {details['grafana']} ({non_operational_grafana} components non-operational)")
        
        # Enhanced Okta processing
        okta_incidents = {}
        try:
            okta_res = await fetch_with_retry(client, "https://feeds.feedburner.com/OktaTrustRSS", "Okta")
            root = ET.fromstring(okta_res.text)
            channel = root.find("channel")
            items = channel.findall("item") if channel is not None else []

            recent_cutoff = datetime.utcnow() - timedelta(hours=48)

            for item in items[:10]:
                title = item.find("title").text if item.find("title") is not None else "Unknown"
                pub_date = item.find("pubDate").text if item.find("pubDate") is not None else ""
                
                try:
                    if pub_date:
                        pub_datetime = datetime.strptime(pub_date, '%a, %d %b %Y %H:%M:%S %z').replace(tzinfo=None)
                    else:
                        pub_datetime = datetime.utcnow()
                except:
                    pub_datetime = datetime.utcnow()
                
                if (pub_datetime > recent_cutoff and 
                    "resolved" not in title.lower() and 
                    "operational" not in title.lower()):
                    
                    if "investigating" in title.lower():
                        incident_status = "investigating"
                    elif "identified" in title.lower():
                        incident_status = "identified"
                    elif "monitoring" in title.lower():
                        incident_status = "monitoring"
                    elif "maintenance" in title.lower():
                        incident_status = "maintenance"
                    else:
                        incident_status = "incident"
                    
                    okta_incidents[title[:100]] = {
                        "status": incident_status,
                        "severity": "incident",
                        "published": pub_date,
                        "age_hours": int((datetime.utcnow() - pub_datetime).total_seconds() / 3600)
                    }
            logger.info(f"Okta incidents found: {len(okta_incidents)}")
        except Exception as e:
            logger.error(f"Error fetching Okta status: {e}")
            okta_incidents = {"Connection Error": {"status": "error", "severity": "critical", "published": "N/A", "description": f"Failed to fetch status: {str(e)[:100]}"}}

        if not okta_incidents:
            okta_incidents = {"All Systems": {"status": "operational", "severity": None, "published": "N/A", "description": "No recent incidents reported"}}

        non_operational_okta = sum(1 for incident in okta_incidents.values() if normalize_status(incident.get("status", "")) != "operational")
        status_colors["okta"] = calculate_status_color(non_operational_okta)
        details["okta"] = "OPERATIONAL" if non_operational_okta == 0 else f"INCIDENTS ({len(okta_incidents)})"
        components["okta"] = okta_incidents
        logger.info(f"Okta status: {details['okta']}")

        # Enhanced Cleverbridge processing
        cleverbridge_incidents = {}
        try:
            cb_res = await fetch_with_retry(client, "https://status.cleverbridge.com/history.rss", "Cleverbridge")
            root = ET.fromstring(cb_res.text)
            channel = root.find("channel")
            items = channel.findall("item") if channel is not None else []
            
            recent_cutoff = datetime.utcnow() - timedelta(hours=48)
            
            for item in items[:10]:
                title = item.find("title").text or "Unknown"
                pub_date_str = item.find("pubDate").text or ""
                
                try:
                    pub_date = datetime.strptime(pub_date_str, '%a, %d %b %Y %H:%M:%S %z').replace(tzinfo=None)
                except:
                    pub_date = datetime.utcnow()
                
                if (pub_date > recent_cutoff and 
                    "resolved" not in title.lower() and
                    "operational" not in title.lower()):
                    
                    if "investigating" in title.lower():
                        incident_status = "investigating"
                    elif "maintenance" in title.lower():
                        incident_status = "maintenance"
                    else:
                        incident_status = "incident"
                    
                    cleverbridge_incidents[title[:100]] = {
                        "status": incident_status,
                        "severity": "incident",
                        "published": pub_date_str,
                        "age_hours": int((datetime.utcnow() - pub_date).total_seconds() / 3600)
                    }
            logger.info(f"Cleverbridge incidents found: {len(cleverbridge_incidents)}")
        except Exception as e:
            logger.error(f"Error fetching Cleverbridge status: {e}")
            cleverbridge_incidents = {"Connection Error": {"status": "error", "severity": "critical", "published": "N/A", "description": f"Failed to fetch status: {str(e)[:100]}"}}

        if not cleverbridge_incidents:
            cleverbridge_incidents = {"All Systems": {"status": "operational", "severity": None, "published": "N/A", "description": "No recent incidents reported"}}

        non_operational_cb = sum(1 for incident in cleverbridge_incidents.values() if normalize_status(incident.get("status", "")) != "operational")
        status_colors["cleverbridge"] = calculate_status_color(non_operational_cb)
        details["cleverbridge"] = "OPERATIONAL" if non_operational_cb == 0 else f"INCIDENTS ({len(cleverbridge_incidents)})"
        components["cleverbridge"] = cleverbridge_incidents
        logger.info(f"Cleverbridge status: {details['cleverbridge']}")

    logger.info("Status data collection completed")
    
    return {
        "details": details,
        "status_colors": status_colors,
        "components": components,
        "timestamp": datetime.utcnow().isoformat(),
        "collection_metadata": {
            "services_monitored": len(MONITORED_SERVICES),
            "total_components": sum(len(components.get(service, {})) for service in MONITORED_SERVICES),
            "collection_duration": "enhanced_monitoring"
        }
    }

async def enhanced_continuous_monitoring():
    """Enhanced continuous monitoring with singleton pattern"""
    global previous_status, monitoring_active
    
    monitoring_active = True
    consecutive_errors = 0
    max_consecutive_errors = 5
    
    logger.info(f"Starting enhanced monitoring every {MONITORING_INTERVAL} seconds")
    logger.info(f"Slack notifications: {'Enabled' if SLACK_WEBHOOK_URL else '✗ Disabled - Set SLACK_WEBHOOK_URL'}")
    logger.info(f"Change confirmation cycles: {CHANGE_CONFIRMATION_CYCLES}")
    logger.info(f"Monitored services: {', '.join(MONITORED_SERVICES)}")
    logger.info(f"Instance ID: {instance_id}")
    
    # Load previous state if available
    await load_monitoring_state()
    
    while monitoring_active:
        try:
            logger.info("=" * 50)
            logger.info(f"Starting monitoring cycle (Instance: {instance_id})")
            
            # Get current status
            current_status = await asyncio.wait_for(
                get_current_status_data(), 
                timeout=120.0
            )
            
            # Initialize previous_status on first run
            if not previous_status:
                previous_status = current_status
                logger.info("Initial status baseline established")
                
                # Log initial status for debugging
                for service in MONITORED_SERVICES:
                    if service in current_status["details"]:
                        logger.info(f"  {service}: {current_status['details'][service]}")
                
                consecutive_errors = 0
                await asyncio.sleep(MONITORING_INTERVAL)
                continue
            
            # Enhanced change detection with detailed logging
            logger.info("Detecting status changes...")
            notifications = detect_enhanced_status_changes(current_status, previous_status)
            
            # Send notifications
            successful_notifications = 0
            failed_notifications = 0
            
            if notifications:
                logger.info(f"Found {len(notifications)} status changes to notify")
                
                for notification in notifications:
                    try:
                        logger.info(f"Sending notification for {notification.service}: {notification.previous_status} → {notification.current_status}")
                        success = await send_enhanced_slack_notification(notification)
                        if success:
                            successful_notifications += 1
                            logger.info(f"Notification sent successfully for {notification.service}")
                        else:
                            failed_notifications += 1
                            logger.warning(f"Notification failed for {notification.service}")
                    except Exception as e:
                        failed_notifications += 1
                        logger.error(f"Failed to send notification for {notification.service}: {e}")
            else:
                logger.info("No status changes detected")
            
            # Update previous status
            previous_status = current_status
            consecutive_errors = 0
            
            # Enhanced logging summary
            total_services = len(current_status.get("details", {}))
            operational_services = sum(1 for status in current_status.get("details", {}).values() 
                                     if normalize_status(status) == "operational")
            
            if notifications:
                logger.info(f"Monitoring cycle complete - {successful_notifications}/{len(notifications)} notifications sent successfully, {failed_notifications} failed")
            else:
                logger.info(f"Monitoring cycle complete - no changes detected")
                
            logger.info(f"Services status: {operational_services}/{total_services} operational")
            
            # Log current status for debugging
            for service in MONITORED_SERVICES:
                if service in current_status["details"]:
                    status = current_status["details"][service]
                    normalized = normalize_status(status)
                    if normalized != "operational":
                        logger.info(f"   {service}: {status} ({normalized})")
                    else:
                        logger.debug(f"  {service}: {status}")
            
            # Save state periodically
            await save_monitoring_state()
            
        except asyncio.TimeoutError:
            consecutive_errors += 1
            logger.error(f"Monitoring cycle timeout (error count: {consecutive_errors})")
        except Exception as e:
            consecutive_errors += 1
            logger.error(f"Error in monitoring cycle: {e} (error count: {consecutive_errors})")
            
            if consecutive_errors >= max_consecutive_errors:
                logger.critical(f"Too many consecutive errors ({consecutive_errors}), sending alert")
                await send_system_alert_notification(f"Monitoring system experiencing issues: {str(e)}")
                consecutive_errors = 0
        
        # Adaptive sleep based on errors
        if consecutive_errors > 0:
            sleep_time = min(MONITORING_INTERVAL * (2 ** consecutive_errors), 1800)
            logger.info(f"Extended sleep due to errors: {sleep_time}s")
            await asyncio.sleep(sleep_time)
        else:
            logger.info(f"Sleeping for {MONITORING_INTERVAL}s until next cycle...")
            await asyncio.sleep(MONITORING_INTERVAL)

async def send_system_alert_notification(message: str):
    """Send system-level alert notification"""
    if not SLACK_WEBHOOK_URL:
        logger.warning("Cannot send system alert - Slack webhook URL not configured")
        return
    
    try:
        payload = {
            "text": "Status Monitor System Alert",
            "attachments": [{
                "color": "danger",
                "title": "Status Monitor System Alert",
                "text": message,
                "footer": "Enhanced Status Monitor - System Alert",
                "ts": int(datetime.utcnow().timestamp())
            }]
        }
        
        timeout = httpx.Timeout(30.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(SLACK_WEBHOOK_URL, json=payload)
            if response.status_code == 200:
                logger.info("System alert notification sent successfully")
            else:
                logger.error(f"Failed to send system alert: {response.status_code}")
        
    except Exception as e:
        logger.error(f"Failed to send system alert: {e}")

async def save_monitoring_state():
    """Save monitoring state to disk"""
    try:
        state = {
            "previous_status": previous_status,
            "notification_history": notification_history[-50:],
            "last_notification_times": {k: v.isoformat() for k, v in last_notification_times.items()},
            "status_change_buffer": {
                k: {
                    **v,
                    'first_seen': v['first_seen'].isoformat() if isinstance(v.get('first_seen'), datetime) else v.get('first_seen')
                } for k, v in status_change_buffer.items()
            },
            "timestamp": datetime.utcnow().isoformat(),
            "instance_id": instance_id
        }
        
        with open("monitoring_state.json", "w", encoding='utf-8') as f:
            json.dump(state, f, indent=2, default=str, ensure_ascii=False)
        
        logger.debug("Monitoring state saved successfully")
    except Exception as e:
        logger.error(f"Failed to save monitoring state: {e}")
async def load_monitoring_state():
    """Load monitoring state from disk"""
    global previous_status, notification_history, last_notification_times, status_change_buffer
    try:
        if os.path.exists("monitoring_state.json"):
            with open("monitoring_state.json", "r", encoding='utf-8') as f:
                state = json.load(f)
            
            # Only load state if it's recent (within last hour)
            try:
                state_timestamp = datetime.fromisoformat(state.get("timestamp", ""))
                if datetime.utcnow() - state_timestamp > timedelta(hours=1):
                    logger.info("Previous state too old, starting fresh")
                    return
            except:
                logger.info("Invalid timestamp in previous state, starting fresh")
                return
            
            previous_status = state.get("previous_status", {})
            notification_history = state.get("notification_history", [])
            
            # Load last notification times
            last_times = state.get("last_notification_times", {})
            last_notification_times = {
                service: datetime.fromisoformat(time_str) 
                for service, time_str in last_times.items()
            }
            
            # Load status change buffer
            buffer_data = state.get("status_change_buffer", {})
            status_change_buffer = {}
            for k, v in buffer_data.items():
                try:
                    status_change_buffer[k] = {
                        **v,
                        'first_seen': datetime.fromisoformat(v['first_seen']) if isinstance(v.get('first_seen'), str) else v.get('first_seen', datetime.utcnow())
                    }
                except:
                    continue
            
            # Convert timestamp strings back to datetime objects
            for entry in notification_history:
                if isinstance(entry.get("timestamp"), str):
                    try:
                        entry["timestamp"] = datetime.fromisoformat(entry["timestamp"])
                    except:
                        entry["timestamp"] = datetime.utcnow()
            
            logger.info(f"Monitoring state loaded successfully (Previous instance: {state.get('instance_id', 'unknown')})")
        else:
            logger.info("No previous monitoring state found, starting fresh")
    except Exception as e:
        logger.error(f"Failed to load monitoring state: {e}")
        previous_status = {}
        notification_history = []
        last_notification_times = {}
        status_change_buffer = {}

# API endpoints (keeping only chatbot - unchanged)
@app.post("/api/chat")
async def professional_chat_endpoint(chat_message: ChatMessage):
    """ENHANCED: Professional incident analysis chat endpoint with live data from official APIs"""
    try:
        # Get both status data and detailed incident information
        status_data = await get_current_status_data()
        incident_data = await fetch_detailed_incident_data()
        
        if not gemini_client:
            operational = sum(1 for s in status_data["details"].values() if normalize_status(s) == "operational")
            total = len(status_data["details"])
            timestamp = status_data['timestamp'][:19].replace('T', ' ')
            
            return ChatResponse(
                response=f"System Overview: {operational}/{total} services operational as of {timestamp} UTC. AI assistant not configured for detailed analysis.",
                sources=[f"Status Dashboard ({timestamp} UTC)"]
            )
        
        # Create comprehensive context for AI
        operational = sum(1 for s in status_data["details"].values() if normalize_status(s) == "operational")
        total = len(status_data["details"])
        timestamp = status_data['timestamp'][:19].replace('T', ' ')
        
        context = f"LIVE STATUS REPORT - {timestamp} UTC\n"
        context += f"Overall: {operational}/{total} services operational\n\n"
        
        # Add detailed incident information from official APIs
        incidents_found = False
        
        for service_name, service_data in incident_data.items():
            if service_name in ["github", "datadog", "jira", "jsm", "prisma", "grafana", "okta", "cleverbridge"]:
                context += f"{service_name.upper()}:\n"
                
                if service_data.get("overall_status") == "incident" or service_data.get("incidents"):
                    incidents_found = True
                    incidents = service_data.get("incidents", [])
                    
                    if incidents:
                        context += f"  Active Incidents ({len(incidents)}):\n"
                        for i, incident in enumerate(incidents[:3], 1):  # Limit to 3 most important
                            context += f"  {i}. {incident.get('name', 'Unknown')}\n"
                            context += f"     Status: {incident.get('status', 'investigating')}\n"
                            context += f"     Impact: {incident.get('impact', 'unknown')}\n"
                            
                            if incident.get('created_at'):
                                try:
                                    created = datetime.fromisoformat(incident['created_at'].replace('Z', '+00:00'))
                                    age = datetime.utcnow().replace(tzinfo=created.tzinfo) - created
                                    hours = int(age.total_seconds() / 3600)
                                    context += f"     Duration: {hours}h ago\n"
                                except:
                                    pass
                            
                            # Add latest update
                            if incident.get('updates') and len(incident['updates']) > 0:
                                latest_update = incident['updates'][0]
                                update_body = latest_update.get('body', '')
                                if update_body and len(update_body) > 100:
                                    update_body = update_body[:200] + "..."
                                context += f"     Latest: {update_body}\n"
                            
                            if incident.get('components'):
                                components = incident['components'][:3]  # Show max 3 components
                                context += f"     Components: {', '.join(components)}\n"
                            
                            context += "\n"
                    else:
                        context += "  Status: Operational\n"
                else:
                    context += "  Status: Operational\n"
                
                # Add region-specific info for Datadog
                if service_name == "datadog" and "regions" in service_data:
                    non_operational_regions = []
                    for region, region_data in service_data["regions"].items():
                        if region_data.get("status") != "none":
                            non_operational_regions.append(f"{region}: {region_data.get('status')}")
                    
                    if non_operational_regions:
                        context += f"  Affected Regions: {', '.join(non_operational_regions[:3])}\n"
                
                context += "\n"
        
        # Add Azure and AWS status if they have issues
        # Add Azure and AWS status with detailed region information
        for cloud_service in ["azure", "aws"]:
            if cloud_service in incident_data and cloud_service in status_data["details"]:
                cloud_data = incident_data[cloud_service]
                service_status = status_data["details"][cloud_service]
                
                context += f"{cloud_service.upper()}:\n"
                context += f"  Overall Status: {cloud_data.get('overall_status', service_status)}\n"
                
                affected_regions = cloud_data.get("affected_regions", [])
                total_affected = cloud_data.get("total_affected_regions", 0)
                
                if affected_regions:
                    context += f"  Affected Regions: {total_affected}\n"
                    context += f"  Top Issues:\n"
                    for region_info in affected_regions[:5]:  # Show top 5
                        context += f"    • {region_info['geography']} - {region_info['region']}: "
                        context += f"{region_info['affected_services']}/{region_info['total_services']} services impacted "
                        context += f"({region_info['status_color']} status)\n"
                else:
                    context += f"  All regions operational\n"
                
                context += "\n"
        
        if not incidents_found:
            context += "All monitored services are currently operational with no active incidents.\n\n"
        
        # Enhanced prompt for better responses
        prompt = f"""{context}



USER QUERY: {chat_message.message}

INSTRUCTIONS:
- Provide a professional, concise response (6-10 lines maximum)
- Focus on live incident data and current status
- If asking about specific services, provide current status and any ongoing incidents
- Include estimated recovery times if mentioned in updates
- For non-operational services, explain the impact and investigation status
- Use official incident data, not generic status labels
- Be specific about which components/regions are affected
- If everything is operational, confirm this clearly

Response:"""

        response = gemini_client.generate_content(prompt)
        ai_response = response.text.strip() if response and response.text else "Unable to generate response from live incident data."
        
        # Create sources list
        sources = [f"Live Status APIs ({timestamp} UTC)"]
        
        # Add specific incident sources if incidents were found
        incident_services = []
        for service_name, service_data in incident_data.items():
            if service_data.get("incidents") or service_data.get("overall_status") == "incident":
                incident_services.append(service_name.title())
        
        if incident_services:
            sources.append(f"Active incidents from: {', '.join(incident_services)}")
        
        return ChatResponse(
            response=ai_response,
            sources=sources
        )
        
    except Exception as e:
        logger.error(f"Enhanced chat error: {str(e)}")
        
        # Fallback response with basic status
        try:
            status_data = await get_current_status_data()
            operational = sum(1 for s in status_data["details"].values() if normalize_status(s) == "operational")
            total = len(status_data["details"])
            
            issues = [service for service, status in status_data["details"].items() 
                     if normalize_status(status) != "operational"]
            
            if issues:
                fallback_response = f"Live data analysis temporarily unavailable. Current status: {operational}/{total} services operational. Services with issues: {', '.join(issues)}. Please check individual service status pages for detailed incident information."
            else:
                fallback_response = f"Live data analysis temporarily unavailable. All {total} monitored services are currently operational."
            
            return ChatResponse(
                response=fallback_response,
                sources=["Fallback Status Check"]
            )
        except:
            return ChatResponse(
                response="Status analysis temporarily unavailable. Please retry or check service status pages directly.",
                sources=["Error Recovery System"]
            )
        
@app.get("/api/debug/full")
async def full_debug():
    """Complete debugging information"""
    import traceback
    
    debug_info = {
        "environment_check": {},
        "gemini_check": {},
        "api_test": {},
        "error": None
    }
    
    # 1. Check environment variables
    debug_info["environment_check"] = {
        "env_file_exists": os.path.exists(".env"),
        "gemini_key_in_env": "GEMINI_API_KEY" in os.environ,
        "gemini_key_length": len(os.getenv("GEMINI_API_KEY", "")) if os.getenv("GEMINI_API_KEY") else 0,
        "gemini_key_preview": os.getenv("GEMINI_API_KEY", "")[:20] + "..." if os.getenv("GEMINI_API_KEY") else "NOT SET",
        "working_directory": os.getcwd(),
        "env_file_path": os.path.join(os.getcwd(), ".env")
    }
    
    # 2. Check Gemini client
    debug_info["gemini_check"] = {
        "client_exists": gemini_client is not None,
        "client_type": str(type(gemini_client)) if gemini_client else "None"
    }
    
    # 3. Try to actually call the API
    try:
        if gemini_client:
            test_response = gemini_client.generate_content("Say 'working' if you can read this")
            debug_info["api_test"] = {
                "success": True,
                "response": test_response.text if test_response else "No response",
                "error": None
            }
        else:
            debug_info["api_test"] = {
                "success": False,
                "response": None,
                "error": "Gemini client is None - API key not loaded"
            }
    except Exception as e:
        debug_info["api_test"] = {
            "success": False,
            "response": None,
            "error": str(e),
            "traceback": traceback.format_exc()
        }
    
    return debug_info

@app.get("/api/debug/models")
async def list_available_models():
    """List all available Gemini models"""
    try:
        import google.generativeai as genai
        
        # List all models
        models = genai.list_models()
        
        available_models = []
        for model in models:
            available_models.append({
                "name": model.name,
                "display_name": model.display_name,
                "description": model.description,
                "supported_methods": model.supported_generation_methods
            })
        
        return {
            "success": True,
            "total_models": len(available_models),
            "models": available_models
        }
    except Exception as e:
        import traceback
        return {
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }
        
# Enhanced Slack API endpoints
@app.get("/api/slack/status")
async def get_enhanced_slack_monitoring_status():
    """Get comprehensive Slack monitoring status with debugging info"""
    global notification_history, status_change_buffer, global_lock
    
    recent_notifications = [
        entry for entry in notification_history
        if datetime.utcnow() - entry.get('timestamp', datetime.utcnow()) < timedelta(hours=24)
    ]
    
    return {
        "monitoring_active": monitoring_active,
        "is_primary_instance": global_lock is not None,
        "instance_id": instance_id,
        "webhook_configured": bool(SLACK_WEBHOOK_URL),
        "webhook_url_preview": SLACK_WEBHOOK_URL[:50] + "..." if SLACK_WEBHOOK_URL and len(SLACK_WEBHOOK_URL) > 50 else SLACK_WEBHOOK_URL or "Not configured",
        "channel": SLACK_CHANNEL,
        "username": SLACK_USERNAME,
        "interval_seconds": MONITORING_INTERVAL,
        "notification_cooldown_seconds": NOTIFICATION_COOLDOWN,
        "change_confirmation_cycles": CHANGE_CONFIRMATION_CYCLES,
        "monitored_services": MONITORED_SERVICES,
        "service_priorities": SERVICE_PRIORITIES,
        "last_check": previous_status.get('timestamp', 'Never') if previous_status else 'Never',
        "notifications_last_24h": len(recent_notifications),
        "pending_changes": len(status_change_buffer),
        "deduplication": {
            "enabled": True,
            "hash_based": True,
            "cooldown_based": True
        },
        "debug_info": {
            "total_notification_history": len(notification_history),
            "last_notification_times_count": len(last_notification_times),
            "current_status_services": list(previous_status.get('details', {}).keys()) if previous_status else [],
            "singleton_lock_active": global_lock is not None
        },
        "recent_notifications": [
            {
                "service": entry.get('service'),
                "status_change": entry.get('status_change'),
                "timestamp": entry.get('timestamp').isoformat() if isinstance(entry.get('timestamp'), datetime) else str(entry.get('timestamp')),
                "hash": entry.get('hash', 'unknown')[:8]  # Show first 8 chars of hash
            } for entry in recent_notifications[-10:]
        ],
        "pending_change_buffer": [
            {
                "key": k,
                "service": k.split('_')[0],
                "target_status": k.split('_')[1] if '_' in k else 'unknown',
                "confirmation_count": v.get('count', 0),
                "required_confirmations": CHANGE_CONFIRMATION_CYCLES,
                "first_seen": v.get('first_seen').isoformat() if isinstance(v.get('first_seen'), datetime) else str(v.get('first_seen'))
            } for k, v in status_change_buffer.items()
        ]
    }

@app.post("/api/slack/test")
async def test_enhanced_slack_notification():
    """Test enhanced Slack notification system with debugging"""
    if not SLACK_WEBHOOK_URL:
        return {"error": "Slack webhook URL not configured", "success": False}
    
    test_notification = EnhancedSlackNotification(
        service="test",
        previous_status="OPERATIONAL",
        current_status="DEGRADED",
        timestamp=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        severity="warning",
        priority="medium",
        components_affected=["Test Component 1", "Test Component 2"],
        duration="testing",
        impact_description="Testing enhanced notification system",
        incident_url="https://status.example.com/test"
    )
    
    try:
        logger.info("Sending test notification...")
        
        # Create test payload directly to bypass rate limiting
        payload = create_enhanced_slack_message(test_notification)
        
        timeout = httpx.Timeout(30.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                SLACK_WEBHOOK_URL,
                json=payload,
                headers={"Content-Type": "application/json"}
            )
            
            success = response.status_code == 200
            
            return {
                "success": success,
                "message": "Enhanced test notification sent successfully" if success else f"Failed to send test notification: {response.status_code}",
                "notification_details": {
                    "service": test_notification.service,
                    "priority": test_notification.priority,
                    "severity": test_notification.severity,
                    "components_count": len(test_notification.components_affected),
                    "has_incident_url": bool(test_notification.incident_url),
                    "notification_hash": create_notification_hash(test_notification)[:8]
                },
                "webhook_url": SLACK_WEBHOOK_URL[:50] + "..." if len(SLACK_WEBHOOK_URL) > 50 else SLACK_WEBHOOK_URL,
                "channel": SLACK_CHANNEL,
                "debug_info": {
                    "webhook_configured": bool(SLACK_WEBHOOK_URL),
                    "monitoring_active": monitoring_active,
                    "is_primary_instance": global_lock is not None,
                    "deduplication_enabled": True,
                    "response_status": response.status_code if 'response' in locals() else None
                }
            }
            
    except Exception as e:
        logger.error(f"Test notification error: {e}")
        return {"error": f"Test notification failed: {str(e)}", "success": False}

@app.post("/api/slack/force-check")
async def force_enhanced_status_check():
    """Force an immediate enhanced status check with proper singleton handling"""
    global previous_status
    
    if not global_lock:
        return {
            "error": "This is not the primary monitoring instance. Only the primary instance can force checks.",
            "success": False,
            "instance_id": instance_id,
            "is_primary": False
        }
    
    try:
        logger.info("Force check initiated via API")
        current_status = await get_current_status_data()
        
        if not previous_status:
            previous_status = current_status
            return {
                "message": "Status initialized, no changes to report", 
                "status": "initialized",
                "timestamp": current_status['timestamp'],
                "services_loaded": list(current_status.get('details', {}).keys()),
                "instance_id": instance_id,
                "is_primary": True
            }
        
        # Log detailed comparison for debugging
        logger.info("Comparing status for force check...")
        for service in MONITORED_SERVICES:
            if service in current_status.get("details", {}) and service in previous_status.get("details", {}):
                current = current_status["details"][service]
                previous = previous_status["details"][service]
                if current != previous:
                    logger.info(f"  {service}: {previous} → {current}")
                else:
                    logger.debug(f"  {service}: no change ({current})")
        
        notifications = detect_enhanced_status_changes(current_status, previous_status)
        
        sent_notifications = []
        failed_notifications = []
        
        for notification in notifications:
            try:
                logger.info(f"Sending force check notification for {notification.service}")
                success = await send_enhanced_slack_notification(notification)
                notification_data = {
                    "service": notification.service,
                    "change": f"{notification.previous_status} → {notification.current_status}",
                    "priority": notification.priority,
                    "severity": notification.severity,
                    "components_affected": len(notification.components_affected),
                    "sent": success,
                    "hash": create_notification_hash(notification)[:8]
                }
                
                if success:
                    sent_notifications.append(notification_data)
                else:
                    failed_notifications.append(notification_data)
                    
            except Exception as e:
                logger.error(f"Failed to send notification for {notification.service}: {e}")
                failed_notifications.append({
                    "service": notification.service,
                    "error": str(e),
                    "sent": False
                })
        
        previous_status = current_status
        
        return {
            "message": f"Force check completed - {len(sent_notifications)} notifications sent, {len(failed_notifications)} failed",
            "successful_notifications": sent_notifications,
            "failed_notifications": failed_notifications,
            "total_changes_detected": len(notifications),
            "timestamp": current_status['timestamp'],
            "monitoring_active": monitoring_active,
            "instance_id": instance_id,
            "is_primary": True
        }
        
    except Exception as e:
        logger.error(f"Force check failed: {e}")
        return {"error": f"Force check failed: {str(e)}", "success": False}

@app.get("/api/status")
async def get_status_api():
    """API endpoint to get current status data"""
    try:
        status_data = await get_current_status_data()
        return status_data
    except Exception as e:
        logger.error(f"Status API error: {e}")
        return {"error": f"Failed to get status data: {str(e)}"}

@app.get("/", response_class=HTMLResponse)
async def enhanced_main_status_page(request: Request):
    """Enhanced main status page"""
    try:
        status_data = await get_current_status_data()
        
        return templates.TemplateResponse("mainstatus3.html", {
            "request": request,
            "details": status_data["details"],
            "status_colors": status_data["status_colors"],
            "components": status_data["components"],
            "enhanced_monitoring": monitoring_active,
            "last_update": status_data["timestamp"]
        })
    except Exception as e:
        logger.error(f"Error in enhanced main page: {e}")
        return templates.TemplateResponse("mainstatus3.html", {
            "request": request,
            "details": {},
            "status_colors": {},
            "components": {},
            "enhanced_monitoring": False,
            "error": str(e)
        })

if __name__ == "__main__":
    import uvicorn
    logger.info("Starting Enhanced Status Monitor with Fixed Slack Integration")
    logger.info(f"Monitoring interval: {MONITORING_INTERVAL} seconds")
    logger.info(f"Slack notifications: {'Enabled' if SLACK_WEBHOOK_URL else 'Disabled'}")
    logger.info(f"Change confirmation cycles: {CHANGE_CONFIRMATION_CYCLES}")
    logger.info(f"Rate limiting: {MAX_NOTIFICATIONS_PER_HOUR}/hour, {NOTIFICATION_COOLDOWN}s cooldown")
    logger.info(f"Monitored services: {', '.join(MONITORED_SERVICES)}")
    logger.info(f"Instance ID: {instance_id}")
    uvicorn.run(app, host="0.0.0.0", port=8000)