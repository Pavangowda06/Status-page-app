# Service Status Dashboard

A real-time monitoring dashboard that tracks the operational status of 10 critical enterprise services with automated Slack notifications and an AI-powered chatbot assistant.

## Overview

This application provides centralized monitoring for:
- **GitHub** - Code repository and CI/CD platform
- **Azure** - Microsoft cloud services
- **AWS** - Amazon Web Services
- **Datadog** - Monitoring and analytics 
- **Jira** - Project management
- **JSM** - Jira Service Management
- **Prisma** - Database toolkit
- **Grafana** - Observability platform
- **Okta** - Identity and access management
- **Cleverbridge** - E-commerce platform

## Key Features

### 1. Real-Time Status Monitoring
- Fetches status from official APIs and RSS feeds every 3 minutes
- Color-coded status indicators (Green/Orange/Red)
- Automatic priority sorting (critical issues appear first)
- Detailed component-level visibility

### 2. Hierarchical Navigation (Azure & AWS)
- Geography-based organization
- Region-specific service groups
- Expandable/collapsible sections
- Service availability statistics

### 3. Automated Slack Notifications
- Real-time alerts for status changes
- Smart deduplication (prevents spam)
- Configurable cooldown periods 
- Priority-based notifications
- Recovery notifications
- Singleton pattern 

### 4. AI-Powered Chatbot
- Gemini 2.5 Flash integration
- Live incident data from official APIs
- AWS/Azure JSON file integration
- Natural language queries
- Context-aware responses

### 5. Enhanced User Experience
- Responsive design (mobile/tablet/desktop)
- Double-click to visit official status pages
- Search functionality (Grafana components)
- Auto-refresh every 5 minutes
- Modal-based detailed views
- Smooth animations and transitions

