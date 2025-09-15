import asyncio
import json
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

# --- MODIFICATION START ---
# This function is updated to match your new requirements.
def parse_service_row(row, headers):
    """
    Parses a service row to extract the status for each region, formatting
    the output as requested:
    - "Available" for good status.
    - "N/A" for not available status.
    - A dictionary with status and severity for any other advisory.
    """
    cells = row.find_all('td')
    if len(cells) < 2:
        return None
        
    service_name = cells[0].get_text().strip()
    statuses = {}
    
    for i, cell in enumerate(cells[1:]):  # Skip first cell (service name)
        if i < len(headers):
            region_name = headers[i]
            hide_text_span = cell.find('span', class_='hide-text')
            
            if hide_text_span:
                status_text = hide_text_span.get_text().strip()
                
                if status_text.lower() == 'good':
                    # If status is good, just use the string 'Available'
                    statuses[region_name] = 'Available'
                elif 'not available' in status_text.lower():
                    # If not available, use the string 'N/A'
                    statuses[region_name] = 'N/A'
                else:
                    # For any other status (e.g., a warning), use the dictionary
                    statuses[region_name] = {"status": status_text, "severity": "Unknown"}
            else:
                # Fallback if the status element can't be found
                statuses[region_name] = 'Unknown'
                
    return service_name, statuses
# --- MODIFICATION END ---


async def get_all_azure_status():
    url = "https://azure.status.microsoft/status"

    print(" launching browser and navigating to page...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(url)
        await page.wait_for_load_state('networkidle')

        print(" scrolling page to load all dynamic content...")
        previous_height = None
        while True:
            current_height = await page.evaluate("document.body.scrollHeight")
            if previous_height == current_height:
                break
            previous_height = current_height
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(1.5)

        print(" fetching page content...")
        content = await page.content()
        await browser.close()

    print(" parsing HTML content with BeautifulSoup...")
    soup = BeautifulSoup(content, 'html.parser')
    structured_status = {}

    main_status_content_wrapper = soup.find('div', id='status-content')
    if not main_status_content_wrapper:
        print("Error: Could not find the main status content wrapper. Aborting.")
        return {}

    geography_elements = main_status_content_wrapper.find_all('li', class_='zone')
    data_zone_to_display_name = {}
    for geo_elem in geography_elements:
        data_zone_name = geo_elem.get('data-zone-name')
        if data_zone_name:
            display_name_tag = geo_elem.find('a')
            cleaned_geography_name = display_name_tag.get_text(strip=True).replace('§', '') if display_name_tag else data_zone_name.replace('-', ' ').title()
            data_zone_to_display_name[data_zone_name] = cleaned_geography_name
            if cleaned_geography_name not in structured_status:
                structured_status[cleaned_geography_name] = {}

    status_tables = main_status_content_wrapper.find_all('table', attrs={'data-zone-name': True})
    if not status_tables:
        print("Warning: No status tables found.")

    for table in status_tables:
        table_data_zone_name = table.get('data-zone-name')
        display_geography_name = data_zone_to_display_name.get(table_data_zone_name, table_data_zone_name.replace('-', ' ').title())

        if display_geography_name not in structured_status:
            structured_status[display_geography_name] = {}

        header_row = table.find('tr', class_='status-table-head')
        regions = [th.get_text().strip() for th in header_row.find_all('th')[1:]] if header_row else []
        if not regions:
            thead = table.find('thead')
            if thead:
                header_cells = thead.find_all('th')
                regions = [th.get_text().strip() for th in header_cells[1:]]

        if not regions:
            continue

        current_group_name = "General" 
        tbody = table.find('tbody')
        if not tbody:
            continue
            
        for row in tbody.find_all('tr'):
            if 'status-category' in row.get('class', []):
                group_cell = row.find('td')
                if group_cell:
                    current_group_name = group_cell.get_text(strip=True)
                continue

            if 'status-table-head' in row.get('class', []) or 'current-incident' in row.get('class', []):
                continue

            parsed = parse_service_row(row, regions)
            if parsed:
                service_name, statuses = parsed
                if not service_name: continue

                for region, status_value in statuses.items():
                    if region and service_name:
                        if region not in structured_status[display_geography_name]:
                            structured_status[display_geography_name][region] = {}
                        
                        if current_group_name not in structured_status[display_geography_name][region]:
                            structured_status[display_geography_name][region][current_group_name] = {}
                        
                        # This line now correctly assigns either a string or a dictionary
                        structured_status[display_geography_name][region][current_group_name][service_name] = status_value
    
    return structured_status

if __name__ == "__main__":
    statuses = asyncio.run(get_all_azure_status())
    
    output_filename = "azure_status_structured.json"
    with open(output_filename, "w", encoding="utf-8") as f:
        json.dump(statuses, f, indent=4, ensure_ascii=False)

    print(f"\n✅ Structured Azure status saved to {output_filename}")