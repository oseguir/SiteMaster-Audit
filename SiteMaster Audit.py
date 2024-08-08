import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import re
import os
import cssutils
from colour import Color
import time
import hashlib
from tenacity import retry, stop_after_attempt, wait_exponential
from html import escape
import datetime

print("\n > Bienvenido a SiteMaster Audit <\n   > Escaneo todo en uno con generación de informe\n\n")

# manejo errores red
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def get_with_retries(url):
    try:
        response = requests.get(url)
        if response.status_code == 429:
            print(f"Rate limit exceeded when accessing {url}")
            raise requests.exceptions.RequestException("Rate limit exceeded")
        return response
    except requests.exceptions.ConnectionError as e:
        print(f"Connection error when accessing {url}: {e}")
        return None  # retorna si es error de conexion
    except requests.exceptions.RequestException as e:
        print(f"Error al realizar la solicitud GET a {url}: {e}")
        raise

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def head_with_retries(url):
    try:
        response = requests.head(url)
        if response.status_code == 429:
            print(f"Rate limit exceeded when accessing {url}")
            raise requests.exceptions.RequestException("Rate limit exceeded")
        return response
    except requests.exceptions.ConnectionError as e:
        print(f"Connection error when accessing {url}: {e}")
        return None  # retorna si es error de conexion
    except requests.exceptions.RequestException as e:
        print(f"Error al realizar la solicitud HEAD a {url}: {e}")
        raise


# ccsutils errores criticos
cssutils.log.setLevel('CRITICAL')

# Elimina las URLs del contenido para evitar falsos positivos en la detección de contenido duplicado.
def clean_content(content):
    url_pattern = r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+'
    return re.sub(url_pattern, '', content)


# Comprobar Open Graph tags y microdatos/schema.org
def check_open_graph_and_schema(soup):
    problems = {
        "Open Graph tags missing": [],
        "Schema.org microdata missing": []
    }
    
    og_tags = ['og:title', 'og:type', 'og:image', 'og:url']     # Comprobar Open Graph tags

    for og_tag in og_tags:
        if not soup.find('meta', property=og_tag):
            problems["Open Graph tags missing"].append(f"Missing Open Graph tag: {og_tag}")

    schema_types = ['Person', 'Organization', 'Product', 'Article', 'Event']     # Comprobar schema.org microdata
    for schema_type in schema_types:
        if not soup.find(attrs={"itemtype": f"http://schema.org/{schema_type}"}):
            problems["Schema.org microdata missing"].append(f"Missing Schema.org type: {schema_type}")

    return problems

# Verificar enlaces internos con parámetros de consulta
def check_internal_links_with_query_params(soup, domain):
    problems = []
    for a_tag in soup.find_all('a', href=True):
        href = a_tag['href']
        link = urljoin(domain, href)
        if urlparse(link).netloc == domain and '?' in urlparse(link).query:
            problems.append(link)
    return problems

#check css i js optimizado
def is_minified(content):
    # Simplificación: Consideramos minificado si el contenido no tiene muchas líneas y no tiene muchos espacios en blanco.
    lines = content.splitlines()
    if len(lines) < 5 and sum(len(line.strip()) for line in lines) / len(content) > 0.9:
        return True
    return False

def check_minification(soup, base_url):
    problems = {
        "CSS no minificado": [],
        "JS no minificado": []
    }
    
    parsed_base_url = urlparse(base_url).netloc

    # Verificar archivos CSS
    for link in soup.find_all('link', rel='stylesheet'):
        href = link.get('href')
        if href and not href.startswith('data:'):
            full_url = urljoin(base_url, href)
            if urlparse(full_url).netloc == parsed_base_url:
                try:
                    response = get_with_retries(full_url)
                    if response.status_code == 200 and not is_minified(response.text):
                        problems["CSS no minificado"].append(href)
                except requests.exceptions.RequestException as e:
                    problems["CSS no minificado"].append(f"{href} (Error: {e})")
    
    # Verificar archivos JS
    for script in soup.find_all('script', src=True):
        src = script.get('src')
        if src and not src.startswith('data:'):
            full_url = urljoin(base_url, src)
            if urlparse(full_url).netloc == parsed_base_url:
                try:
                    response = get_with_retries(full_url)
                    if response.status_code == 200 and not is_minified(response.text):
                        problems["JS no minificado"].append(src)
                except requests.exceptions.RequestException as e:
                    problems["JS no minificado"].append(f"{src} (Error: {e})")

    return problems

#check idioma
def check_language_attribute(soup):
    html_tag = soup.find('html')
    if html_tag and html_tag.get('lang'):
        return True, html_tag.get('lang')
    return False, None

# Comprobar contenido duplicado
def get_page_content_hash(soup):
    # Extraer el contenido textual de la página
    content = soup.get_text()
    # Generar un hash del contenido
    content_hash = hashlib.md5(content.encode('utf-8')).hexdigest()
    return content_hash

def check_duplicate_content(internal_links):
    content_hashes = {}
    urls_checked = []

    for url in internal_links:
        try:
            response = get_with_retries(url)
            if response:
                soup = BeautifulSoup(response.content, 'html.parser')
                text_content = soup.get_text(separator=' ', strip=True)
                cleaned_content = clean_content(text_content)
                content_hash = hashlib.md5(cleaned_content.encode('utf-8')).hexdigest()
                content_hashes[url] = content_hash
                urls_checked.append(url)
        except Exception as e:
            print(f"Error al analizar el contenido de {url}: {e}")

    duplicate_content_issues = []
    for i in range(len(urls_checked)):
        for j in range(i + 1, len(urls_checked)):
            if content_hashes[urls_checked[i]] == content_hashes[urls_checked[j]]:
                duplicate_content_issues.append((urls_checked[i], urls_checked[j]))

    return duplicate_content_issues

# Comprueba tiempo en cargarse
def get_page_load_time(url):
    start_time = time.time()
    response = get_with_retries(url)
    load_time = time.time() - start_time
    return load_time

# Verificar el uso de ARIA roles en elementos interactivos
def check_aria_roles(soup):
    problems = []
    # Elementos interactivos por defecto que no requieren rol ARIA
    interactive_elements = ['button', 'a', 'input', 'textarea', 'select', 'option']

    # Buscar todos los elementos
    elements = soup.find_all(True)

    for element in elements:
        # Si el elemento tiene algún atributo ARIA, no lo consideramos un problema
        if any(attr.startswith('aria-') for attr in element.attrs):
            continue
        
        # Si el elemento es interactivo por defecto y no tiene un rol ARIA, no lo consideramos un problema
        if element.name in interactive_elements:
            continue
        
        # Verificar si el elemento tiene un evento interactivo que lo haría relevante para ARIA roles
        if (element.get('onclick') or element.get('onkeydown') or element.get('onkeypress')):
            element_info = f"{element.name} | Class: {element.get('class')}" if element.get('class') else f"{element.name} | ID: {element.get('id')}"
            problems.append(f"Elemento interactivo sin rol ARIA: {element_info}")

    return problems


# Verificar accesibilidad de tablas
def check_table_accessibility(soup):
    problems = []
    tables = soup.find_all('table')
    for table in tables:
        if not table.find('thead'):
            problems.append(f"Tabla sin thead: {str(table)[:100]}")
        if not table.find('tbody'):
            problems.append(f"Tabla sin tbody: {str(table)[:100]}")
        th_elements = table.find_all('th')
        for th in th_elements:
            if not th.get('scope'):
                problems.append(f"th sin scope: {str(th)[:100]}")
    return problems

color_names = {
    'black': '#000000',
    'white': '#ffffff',
    'red': '#ff0000',
    'lime': '#00ff00',
    'blue': '#0000ff',
    'yellow': '#ffff00',
    'cyan': '#00ffff',
    'magenta': '#ff00ff',
    'silver': '#c0c0c0',
    'gray': '#808080',
    'maroon': '#800000',
    'olive': '#808000',
    'green': '#008000',
    'purple': '#800080',
    'teal': '#008080',
    'navy': '#000080'
}
# Function to calculate the contrast ratio
def contrast_ratio(color1, color2):
    l1 = color1.get_luminance() + 0.05
    l2 = color2.get_luminance() + 0.05
    if l1 > l2:
        return l1 / l2
    else:
        return l2 / l1
    
# Function to convert hex color to Color object
def hex_to_color(color_value):
    try:
        # Convertir nombres de colores a hexadecimales si es necesario
        if color_value in color_names:
            color_value = color_names[color_value]
        
        # Asegurar que el valor de color esté en el formato correcto
        if not color_value.startswith('#'):
            color_value = f'#{color_value}'
        
        return Color(color_value)
    except ValueError:
        return None  # Devolver None si el color no es válido

def get_style_property(element, property):
    style = element.get('style')
    if style:
        styles = style.split(';')
        for s in styles:
            if property in s:
                return s.split(':')[1].strip()
    return None

def check_font_size_and_contrast(soup):
    problems = {
        "Elementos con tamaño de fuente menor a 16px": [],
        "Elementos con buen contraste de color": []
    }

    # Extraer y analizar estilos CSS
    styles = soup.find_all('style')
    css_parser = cssutils.CSSParser(fetcher=None)
    css_rules = {}

    for style in styles:
        css = css_parser.parseString(style.string)
        for rule in css:
            if rule.type == rule.STYLE_RULE:
                for property in rule.style:
                    selector = rule.selectorText
                    css_rules.setdefault(selector, {})[property.name] = property.value

    # Extraer estilos en línea y específicos de etiquetas
    elements = soup.find_all(['p', 'span', 'a', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'ul'])
    for element in elements:
        # Saltar elementos dentro del ID footer-page
        if element.find_parent(id='footer-page'):
            continue

        style = element.get('style')
        computed_styles = css_rules.get(element.name, {})
        if style:
            inline_styles = css_parser.parseStyle(style)
            for property in inline_styles:
                computed_styles[property.name] = property.value

        font_size = computed_styles.get('font-size', '16px')
        color = computed_styles.get('color', '#000000')
        background_color = computed_styles.get('background-color', '#ffffff')

        font_size_value = int(re.match(r'(\d+)', font_size).group(1))
        if font_size_value < 16:
            text_content = element.text.strip()
            if text_content:
                problems["Elementos con tamaño de fuente menor a 16px"].append(f"Texto: {text_content} | Tamaño de fuente: {font_size}")

        # Calcular la relación de contraste
        color_obj = hex_to_color(color)
        background_color_obj = hex_to_color(background_color)

        if color_obj and background_color_obj:
            contrast = contrast_ratio(color_obj, background_color_obj)
            if contrast > 2:
                text_content = element.text.strip()
                if text_content:
                    problems["Elementos con buen contraste de color"].append(f"Texto: {text_content} | Contraste: {contrast:.2f}")
        else:
            problems["Elementos con buen contraste de color"].append(f"Elemento {element.name} tiene colores no válidos para calcular el contraste: color {color}, fondo {background_color}")

    return problems


# Verificar elementos interactivos con eventos de teclado
def check_keyboard_accessibility(soup):
    problems = []
    interactive_elements = soup.find_all(['button', 'a', 'input', 'textarea', 'select'])
    for element in interactive_elements:
        if not element.has_attr('tabindex') and not element.has_attr('onkeypress'):
            element_info = f"Tag: {element.name}, Text: {element.get_text(strip=True)}, Attributes: {element.attrs}"
            problems.append(f"Elemento interactivo no accesible con teclado: {element_info}")
    return problems


# Verificar el uso de landmarks ARIA
def check_aria_landmarks(soup):
    landmarks = ['banner', 'navigation', 'main', 'contentinfo']
    problems = []
    for landmark in landmarks:
        if not soup.find(attrs={"role": landmark}):
            problems.append(f"Falta ARIA landmark: {landmark}")
    return problems

# Verificar la estructura semántica del documento
def check_semantic_structure(soup):
    problems = []
    required_tags = ['header', 'nav', 'main', 'footer']
    for tag in required_tags:
        if not soup.find(tag):
            problems.append(f"Falta el elemento semántico: {tag}")
    return problems

# Verificar la accesibilidad de formularios
def check_form_accessibility(soup):
    problems = []
    forms = soup.find_all('form')
    for form in forms:
        inputs = form.find_all(['input', 'textarea', 'select'])
        labels = form.find_all('label')
        for input_elem in inputs:
            if 'id' in input_elem.attrs:
                label_for_input = [label for label in labels if label.get('for') == input_elem['id']]
                if not label_for_input and not input_elem.get('aria-label'):
                    problems.append(f"Campo de formulario sin etiqueta o aria-label: {str(input_elem)[:100]}")
            if not input_elem.get('aria-describedby') and not input_elem.get('aria-label'):
                problems.append(f"Campo de formulario sin aria-describedby ni aria-label: {str(input_elem)[:100]}")
    return problems

# Verificar la ausencia de contenido parpadeante
def check_no_blinking_content(soup):
    problems = []
    blinking_elements = soup.find_all(style=re.compile(r'animation.*blink'))
    for element in blinking_elements:
        problems.append(f"Elemento con contenido parpadeante: {str(element)[:100]}")
    return problems

# Verificar el uso de títulos y descripciones en los SVGs
def check_svg_accessibility(soup):
    problems = []
    svgs = soup.find_all('svg')
    for svg in svgs:
        if not svg.find('title'):
            problems.append(f"SVG sin título: {str(svg)[:100]}")
        if not svg.find('desc'):
            problems.append(f"SVG sin descripción: {str(svg)[:100]}")
    return problems

def create_individual_html_report(report, output_path):
    severity_styles = {
        'high': 'background-color: red; color: white;',
        'medium': 'background-color: orange; color: black;',
        'low': 'background-color: gold; color: black;',
        'good': 'background-color:#8ede3e; color: black;'
    }

    severity_mapping = {
        'Imágenes sin texto alternativo': 'high',
        'Campos de formulario sin etiquetas o aria-label': 'high',
        'Botones sin texto': 'high',
        'iFrames sin título': 'high',
        'Elementos interactivos sin roles ARIA': 'high',
        'Elementos con tamaño de fuente menor a 16px': 'medium',
        'Enlaces rotos (404)': 'high',
        'Tiempo de carga de la página': 'medium',
        'URLs demasiado largas': 'medium',
        'Canonical tags': 'medium',
        'Hreflang tags': 'medium',
        'Accesibilidad de tablas': 'high',
        'Uso de roles ARIA': 'high',
        'Posible contenido duplicado': 'high',
        'Eventos de teclado': 'high',
        'Landmarks ARIA': 'medium',
        'Estructura semántica del documento': 'medium',
        'Accesibilidad de formularios': 'high',
        'Contenido parpadeante': 'high',
        'Accesibilidad de SVGs': 'high',
        'Open Graph tags': 'low',
        'Enlaces internos con parámetros de consulta': 'low',
        'Elementos con buen contraste de color': 'good'
    }

    html_content = f'''
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Accessibility and SEO Audit Report for {report['url']}</title>
        <style>
            body {{ font-family: Arial, sans-serif; }}
            .accordion {{ cursor: pointer; padding: 18px; width: 100%; text-align: left; border: none; outline: none; transition: 0.4s; background-color: #eee; }}
            .accordion:hover, .active {{ background-color: #ccc; }}
            .panel {{ padding: 0 18px; display: none; background-color: white; overflow: hidden; }}
            .panel p {{ margin: 0; padding: 10px 0; border-bottom: 1px solid #ddd; }}
            .problem-list {{ margin: 0; padding-left: 20px; list-style-type: decimal; }}
        </style>
    </head>
    <body>
        <h1>Informe de auditoría de accesibilidad y SEO</h1>
        <h2>URL: {report['url']}</h2>
        <button class="accordion">Heading Hierarchy ({'Error' if report['heading_issues'] else 'No Errors'})</button>
        <div class="panel"><pre>{report['heading_hierarchy']}</pre></div>
    '''

    for category, items in report['problems'].items():
        if items:
            severity = severity_mapping.get(category, 'low')
            style = severity_styles[severity]
            html_content += f'<button class="accordion" style="{style}">{category} ({len(items)})</button><div class="panel">'
            if category == "Eventos de teclado":
                html_content += '<ul class="problem-list">'
                for item in items:
                    html_content += f'<li>{escape(item)}</li>'
                html_content += '</ul>'
            else:
                for item in items:
                    html_content += f'<p>{escape(item)}</p>'
            html_content += '</div>'

    html_content += '''
        <script>
            var acc = document.getElementsByClassName("accordion");
            for (var i = 0; i < acc.length; i++) {
                acc[i].addEventListener("click", function() {
                    this.classList.toggle("active");
                    var panel = this.nextElementSibling;
                    if (panel.style.display === "block") {
                        panel.style.display = "none";
                    } else {
                        panel.style.display = "block";
                    }
                });
            }
        </script>
    </body>
    </html>
    '''

    with open(output_path, 'w', encoding='utf-8') as file:
        file.write(html_content)


def combine_html_reports(report_files, final_output_path, general_info):
    severity_styles = {
        'high': 'background-color: red; color: white;',
        'medium': 'background-color: orange; color: black;',
        'low': 'background-color: gold; color: black;'
    }

    severity_mapping = {
        'Imágenes sin texto alternativo': 'high',
        'Campos de formulario sin etiquetas o aria-label': 'high',
        'Botones sin texto': 'high',
        'iFrames sin título': 'high',
        'Elementos interactivos sin roles ARIA': 'high',
        'Elementos con tamaño de fuente menor a 16px': 'medium',
        'Elementos con buen contraste de color': 'high',
        'Enlaces rotos (404)': 'high',
        'Tiempo de carga de la página': 'medium',
        'URLs demasiado largas': 'medium',
        'Canonical tags': 'medium',
        'Hreflang tags': 'medium',
        'Accesibilidad de tablas': 'high',
        'Uso de roles ARIA': 'high',
        'Posible contenido duplicado': 'high',
        'Eventos de teclado': 'high',
        'Landmarks ARIA': 'medium',
        'Estructura semántica del documento': 'medium',
        'Accesibilidad de formularios': 'high',
        'Contenido parpadeante': 'high',
        'Accesibilidad de SVGs': 'high',
        'Open Graph tags': 'low',
        'Enlaces internos con parámetros de consulta': 'low'
    }

    combined_html_content = f'''
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Combined Accessibility and SEO Audit Report</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 0; padding: 0; }}
            .container {{ padding: 20px; }}
            .accordion {{ cursor: pointer; padding: 18px; width: 100%; text-align: left; border: none; outline: none; transition: 0.4s; background-color: #eee; margin-bottom: 10px; }}
            .accordion:hover, .active {{ background-color: #ccc; }}
            .panel {{ padding: 0 18px; display: none; background-color: white; overflow: hidden; }}
            .panel p {{ margin: 0; padding: 10px 0; border-bottom: 1px solid #ddd; }}
            .problem-list {{ margin: 0; padding-left: 20px; list-style-type: decimal; }}
            .url-accordion {{ background-color: #f6f6f6; color: black; margin-bottom: 10px; }}
            pre {{ white-space: pre-wrap; word-wrap: break-word; }}

            /* Responsive adjustments */
            @media (max-width: 600px) {{
                .accordion, .url-accordion {{ padding: 10px; font-size: 14px; }}
                .panel p {{ padding: 5px 0; }}
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Informe combinado de auditoría de accesibilidad y SEO - SiteMaster Audit </h1>
            <button class="accordion">Información General</button>
            <div class="panel">
                <pre>{escape(general_info)}</pre>
            </div>
    '''

    accordion_id_counter = 0

    for report_file in report_files:
        with open(report_file, 'r', encoding='utf-8') as file:
            soup = BeautifulSoup(file, 'html.parser')
            body_content = soup.find('body').decode_contents()

            # Extraer el título y la URL
            url_title = soup.find('h2')
            url_header = f'<h2>{url_title.text}</h2>' if url_title else ''

            body_soup = BeautifulSoup(body_content, 'html.parser')
            accordions = body_soup.find_all(class_='accordion')
            panels = body_soup.find_all(class_='panel')

            combined_html_content += f'''
            <button class="accordion url-accordion">{url_header}</button>
            <div class="panel">
            '''

            sorted_accordions_panels = sorted(zip(accordions, panels), key=lambda x: severity_mapping.get(x[0].text.split()[0], 'low'), reverse=True)

            for i, (accordion, panel) in enumerate(sorted_accordions_panels):
                unique_id = f'{accordion_id_counter}-{i}'
                accordion['id'] = f'accordion-{unique_id}'
                panel['id'] = f'panel-{unique_id}'
                combined_html_content += str(accordion)
                combined_html_content += str(panel)
                accordion_id_counter += 1

            combined_html_content += '</div>'

    combined_html_content += '''
        </div>
        <script>
            document.addEventListener("DOMContentLoaded", function() {
                var acc = document.getElementsByClassName("accordion");
                for (var i = 0; i < acc.length; i++) {
                    acc[i].addEventListener("click", function() {
                        this.classList.toggle("active");
                        var panel = this.nextElementSibling;
                        if (panel.style.display === "block") {
                            panel.style.display = "none";
                        } else {
                            panel.style.display = "block";
                        }
                    });
                }
            });
        </script>
    </body>
    </html>
    '''

    with open(final_output_path, 'w', encoding='utf-8') as file:
        file.write(combined_html_content)

    for report_file in report_files:
        os.remove(report_file)


def check_sitemap(url):
    parsed_url = urlparse(url)
    if not parsed_url.scheme or not parsed_url.netloc:
        print(f"Invalid URL: {url}")
        return False, False, None

    base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
    sitemap_url = base_url + '/sitemap.xml'
    sitemap_index_url = base_url + '/sitemap_index.xml'
    
    try:
        sitemap_exists = requests.head(sitemap_url).status_code == 200
    except requests.exceptions.RequestException as e:
        print(f"Error al acceder a {sitemap_url}: {e}")
        sitemap_exists = False
    
    try:
        sitemap_index_exists = requests.head(sitemap_index_url).status_code == 200
    except requests.exceptions.RequestException as e:
        print(f"Error al acceder a {sitemap_index_url}: {e}")
        sitemap_index_exists = False
    
    return sitemap_exists, sitemap_index_exists, sitemap_url if sitemap_exists else sitemap_index_url if sitemap_index_exists else None


def check_robots(url):
    base_url = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    robots_url = base_url + '/robots.txt'
    robots_exists = requests.head(robots_url).status_code == 200
    return robots_exists, robots_url


def get_internal_links(url, soup):
    domain = urlparse(url).netloc
    links = []
    for a_tag in soup.find_all('a', href=True):
        href = a_tag['href']
        link = urljoin(url, href)
        if urlparse(link).netloc == domain and not re.search(r'\.(webp|jpg|jpeg|png)$', link):
            links.append(link)
    return set(links)

# verifica long url
def check_url_length(url):
    if len(url) > 100:  # Consideramos problemáticas las URLs mayores a 100 caracteres
        return True, len(url)
    return False, len(url)

#Verificar la presencia de canonical tags
def check_canonical_tag(soup):
    canonical_tag = soup.find('link', rel='canonical')
    if not canonical_tag:
        return False
    return True

#check hreflang
def check_hreflang_tags(soup):
    hreflang_tags = soup.find_all('link', rel='alternate', hreflang=True)
    if not hreflang_tags:
        return False
    return True

# Accesibilidad ##########################################
# Verificar elementos interactivos con eventos de teclado
def check_keyboard_accessibility(soup):
    problems = []
    interactive_elements = soup.find_all(['button', 'a', 'input', 'textarea', 'select'])
    for element in interactive_elements:
        if not element.has_attr('tabindex') and not element.has_attr('onkeypress'):
            element_info = str(element)[:100]
            problems.append(f"Elemento interactivo no accesible con teclado: {element_info}")
    return problems

# Verificar el uso de landmarks ARIA
def check_aria_landmarks(soup):
    landmarks = ['banner', 'navigation', 'main', 'contentinfo']
    problems = []
    for landmark in landmarks:
        if not soup.find(attrs={"role": landmark}):
            problems.append(f"Falta ARIA landmark: {landmark}")
    return problems

# Verificar la estructura semántica del documento
def check_semantic_structure(soup):
    problems = []
    required_tags = ['header', 'nav', 'main', 'footer']
    for tag in required_tags:
        if not soup.find(tag):
            problems.append(f"Falta el elemento semántico: {tag}")
    return problems

# Verificar la accesibilidad de formularios

def check_keyboard_events(soup):
    problems = []
    interactive_elements = soup.find_all(['button', 'a', 'input', 'textarea', 'select'])
    for element in interactive_elements:
        if not element.get('tabindex') and not element.get('onkeydown'):
            element_info = str(element)[:100]  # Truncar para evitar largos outputs
            problems.append(f"Elemento interactivo sin eventos de teclado: {element_info}")
    return problems

def check_form_accessibility(soup):
    problems = []
    forms = soup.find_all('form')
    for form in forms:
        inputs = form.find_all(['input', 'textarea', 'select'])
        labels = form.find_all('label')
        for input_elem in inputs:
            if 'id' in input_elem.attrs:
                label_for_input = [label for label in labels if label.get('for') == input_elem['id']]
                if not label_for_input and not input_elem.get('aria-label'):
                    problems.append(f"Campo de formulario sin etiqueta o aria-label: {str(input_elem)[:100]}")
            if not input_elem.get('aria-describedby') and not input_elem.get('aria-label'):
                problems.append(f"Campo de formulario sin aria-describedby ni aria-label: {str(input_elem)[:100]}")
    return problems

# Verificar la ausencia de contenido parpadeante
def check_no_blinking_content(soup):
    problems = []
    blinking_elements = soup.find_all(style=re.compile(r'animation.*blink'))
    for element in blinking_elements:
        problems.append(f"Elemento con contenido parpadeante: {str(element)[:100]}")
    return problems

# Verificar el uso de títulos y descripciones en los SVGs
def check_svg_accessibility(soup):
    problems = []
    svgs = soup.find_all('svg')
    for svg in svgs:
        if not svg.find('title'):
            problems.append(f"SVG sin título: {str(svg)[:100]}")
        if not svg.find('desc'):
            problems.append(f"SVG sin descripción: {str(svg)[:100]}")
    return problems

def analyze_page(url, internal_links=None):
    load_time = get_page_load_time(url)
    
    try:
        response = get_with_retries(url)
    except requests.exceptions.RequestException as e:
        return {"error": f"Error al acceder a la página: {e}"}
    
    if response.status_code != 200:
        return {"error": f"Error al acceder a la página: {response.status_code}"}
    
    soup = BeautifulSoup(response.content, 'html.parser')
    
    # Excluir elementos dentro de #footer-page
    footer_page = soup.find(id='footer-page')
    if (footer_page):
        footer_page.decompose()

    problems = {
        "Imágenes sin texto alternativo": [],
        "Imágenes no WebP": [],
        "Campos de formulario sin etiquetas o aria-label": [],
        "Problemas en la jerarquía de encabezados": [],
        "Botones sin texto": [],
        "iFrames sin título": [],
        "Elementos interactivos sin roles ARIA": [],
        "Elementos con tamaño de fuente menor a 16px": [],
        "Elementos con buen contraste de color": [],
        "Enlaces rotos (404)": [],
        "Tiempo de carga de la página": [],
        "URLs demasiado largas": [],
        "Canonical tags": [],
        "Hreflang tags": [],
        "Accesibilidad de tablas": [],
        "Uso de roles ARIA": [],
        "Posible contenido duplicado": [],
        "Eventos de teclado": [],
        "Landmarks ARIA": [],
        "Estructura semántica del documento": [],
        "Accesibilidad de formularios": [],
        "Contenido parpadeante": [],
        "Accesibilidad de SVGs": []
    }

    seo_info = []
    aria_roles_info = {
        "total_aria_roles": 0,
        "total_roles": 0
    }

    # Verificar el tiempo de carga
    if load_time > 3:  # por ejemplo, consideramos problemático un tiempo mayor a 3 segundos
        problems["Tiempo de carga de la página"].append(f"Tiempo de carga: {load_time:.2f} segundos")
    
    # Verificar la longitud de la URL
    is_long, url_length = check_url_length(url)
    if is_long:
        problems["URLs demasiado largas"].append(f"URL: {url} | Longitud: {url_length} caracteres")

    # Verificar canonical tags
    if not check_canonical_tag(soup):
        problems["Canonical tags"].append("No se encontraron etiquetas canonical")

    # Verificar hreflang tags
    if not check_hreflang_tags(soup):
        problems["Hreflang tags"].append("No se encontraron etiquetas hreflang")
    
    # Verificar el uso de roles ARIA
    aria_roles = check_aria_roles(soup)
    aria_roles_info["total_aria_roles"] = len(soup.find_all(attrs={"role": True}))
    aria_roles_info["total_roles"] = sum(1 for _ in soup.find_all(attrs={"role": True}))

    # Verificar accesibilidad de tablas
    table_problems = check_table_accessibility(soup)
    if table_problems:
        problems["Accesibilidad de tablas"].extend(table_problems)
    
    # Verificar contenido duplicado si se proporcionan enlaces internos
    if internal_links:
        duplicates = check_duplicate_content(internal_links)
        if duplicates:
            problems["Posible contenido duplicado"].extend([f"{dup[0]} y {dup[1]}" for dup in duplicates])
    
    # Verificar eventos de teclado
    keyboard_problems = check_keyboard_events(soup)
    if keyboard_problems:
        problems["Eventos de teclado"].extend([f"Falla en <{elem}>" for elem in keyboard_problems])
    
    # Verificar landmarks ARIA
    landmarks_problems = check_aria_landmarks(soup)
    if landmarks_problems:
        problems["Landmarks ARIA"].extend(landmarks_problems)
    
    # Verificar estructura semántica
    semantic_problems = check_semantic_structure(soup)
    if semantic_problems:
        problems["Estructura semántica del documento"].extend(semantic_problems)
    
    # Verificar accesibilidad de formularios
    form_problems = check_form_accessibility(soup)
    if form_problems:
        problems["Accesibilidad de formularios"].extend(form_problems)
    
    # Verificar contenido parpadeante
    blinking_problems = check_no_blinking_content(soup)
    if blinking_problems:
        problems["Contenido parpadeante"].extend(blinking_problems)
    
    # Verificar accesibilidad de SVGs
    svg_problems = check_svg_accessibility(soup)
    if svg_problems:
        problems["Accesibilidad de SVGs"].extend(svg_problems)
    
    # Obtener título de la página
    title_tag = soup.find('title')
    title = title_tag.text.strip() if title_tag else "No se encontró título"
    seo_info.append(f"Título: {title}")
    if not title_tag:
        problems["Título de la página"] = ["No se encontró título"]
    elif len(title) < 30 or len(title) > 60:
        problems["Título de la página"] = [f"La longitud del título es {'demasiado corta' if len(title) < 30 else 'demasiado larga'}: {len(title)} caracteres"]

    # Obtener meta description
    meta_description_tag = soup.find('meta', attrs={'name': 'description'})
    meta_description = meta_description_tag['content'].strip() if meta_description_tag else "No se encontró meta descripción"
    seo_info.append(f"Meta Descripción: {meta_description}")
    if not meta_description_tag:
        problems["Meta Descripción"] = ["No se encontró meta descripción"]
    elif len(meta_description) < 70 or len(meta_description) > 160:
        problems["Meta Descripción"] = [f"La longitud de la meta descripción es {'demasiado corta' if len(meta_description) < 70 else 'demasiado larga'}: {len(meta_description)} caracteres"]

    # Verificar presencia de H1
    h1_tags = soup.find_all('h1')
    if len(h1_tags) == 0:
        problems["H1"] = ["No se encontraron etiquetas h1"]
    elif len(h1_tags) > 1:
        problems["H1"] = [f"Multiples etiquetas H1 encontradas: {len(h1_tags)}"]
    
    # Verificar enlaces internos y externos
    internal_links = [a['href'] for a in soup.find_all('a', href=True) if url in a['href']]
    external_links = [a['href'] for a in soup.find_all('a', href=True) if url not in a['href']]
    seo_info.append(f"Internal Links: {len(internal_links)}")
    seo_info.append(f"External Links: {len(external_links)}")
    
    # Verificar enlaces rotos (404)
    for link in soup.find_all('a', href=True):
        link_url = link['href']
        try:
            if link_url.startswith(url) and head_with_retries(link_url).status_code == 404:
                problems["Enlaces rotos (404)"].append(link_url)
        except requests.exceptions.RequestException:
            continue
    
    # Verificar presencia de favicon
    favicon = soup.find('link', rel='icon')
    if not favicon:
        problems["Favicon"] = ["No se encontró ningún favicon (icon de url)"]
    
    # Verificar etiqueta robots
    robots_exists, robots_url = check_robots(url)
    try:
        robots_content = requests.get(robots_url).text if robots_exists else "No se encontró ningún archivo robots.txt"
    except requests.exceptions.RequestException:
        robots_content = "No se encontró ningún archivo robots.txt"
    if not robots_exists:
        problems["Robots"] = ["No se encontró ningún archivo robots.txt"]
    
    # Verificar sitemap.xml y sitemap_index.xml
    sitemap_exists, sitemap_index_exists, sitemap_url = check_sitemap(url)
    if not sitemap_exists and not sitemap_index_exists:
        problems["Sitemap"] = ["No se encontró sitemap.xml ni sitemap_index.xml"]
    
    seo_info.append(f"Robots URL: {robots_url}")
    seo_info.append(f"Sitemap URL: {sitemap_url}")
    
    # Verificar Google Analytics
    google_analytics = bool(soup.find(string=re.compile("GoogleAnalyticsObject")))
    seo_info.append(f"Google Analytics: {'Encontrado' if google_analytics else 'No encontrado'}")
    if not google_analytics:
        problems["Google Analytics"] = ["Google Analytics no encontrado"]
    
    # Verificar etiquetas meta keywords
    meta_keywords_tag = soup.find('meta', attrs={'name': 'keywords'})
    meta_keywords = meta_keywords_tag['content'].strip() if meta_keywords_tag else "No se encontraron palabras clave meta"
    seo_info.append(f"Meta Keywords: {meta_keywords}")
    if not meta_keywords_tag:
        problems["Meta Keywords"] = ["No se encontraron palabras clave meta"]
    
    # Verificar imágenes sin texto alternativo
    images_without_alt = [str(img) for img in soup.find_all('img') if not img.get('alt')]
    if images_without_alt:
        problems["Imágenes sin texto alternativo"] = images_without_alt
    
    # Verificar imágenes que no son WebP y no contienen 'logo' o 'plugin' en la URL
    images_not_webp = [img['src'] for img in soup.find_all('img') if 'src' in img.attrs and not img['src'].endswith('.webp') and not any(kw in img['src'] for kw in ['logo', 'plugin'])]
    if images_not_webp:
        problems["Imágenes no WebP"] = images_not_webp
    
    # Verificar formularios sin etiquetas de campo o aria-label
    for form in soup.find_all('form'):
        inputs = form.find_all(['input', 'textarea', 'select'])
        labels = form.find_all('label')
        for input_elem in inputs:
            if 'id' in input_elem.attrs:
                label_for_input = [label for label in labels if label.get('for') == input_elem['id']]
                if not label_for_input and not input_elem.get('aria-label'):
                    problems["Campos de formulario sin etiquetas o aria-label"].append(str(input_elem))
    
    # Verificar jerarquía de encabezados
    headings = soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6'])
    heading_order = [(h.name, h.text.strip()) for h in headings]
    heading_hierarchy = "\n".join([f"{name}: {text}" for name, text in heading_order])
    heading_issues = False
    for i in range(len(headings) - 1):
        if int(headings[i+1].name[1]) > int(headings[i].name[1]) + 1:
            problems["Problemas en la jerarquía de encabezados"].append("Desorden en la jerarquía de encabezados")
            heading_issues = True
            break
    
    # Verificar botones sin texto
    buttons_without_text = [str(button) for button in soup.find_all('button') if not button.text.strip()]
    if buttons_without_text:
        problems["Botones sin texto"] = buttons_without_text
    
    # Verificar iframes sin títulos
    iframes_without_title = [str(iframe) for iframe in soup.find_all('iframe') if not iframe.get('title')]
    if iframes_without_title:
        problems["iFrames sin título"] = iframes_without_title
    
    # Verificar elementos interactivos sin roles ARIA
    for tag in ['button', 'input', 'select', 'textarea']:
        elements = soup.find_all(tag)
        for elem in elements:
            if not elem.get('role') and not any(attr.startswith('aria-') for attr in elem.attrs):
                elem_text = elem.text.strip() or f"Class: {elem.get('class')}" or f"ID: {elem.get('id')}"
                if elem_text:
                    problems["Elementos interactivos sin roles ARIA"].append(elem_text)
    
    # Verificar contraste de colores y tamaño de fuente en textos específicos
    font_and_contrast_problems = check_font_size_and_contrast(soup)
    problems["Elementos con tamaño de fuente menor a 16px"].extend(font_and_contrast_problems["Elementos con tamaño de fuente menor a 16px"])
    problems["Elementos con buen contraste de color"].extend(font_and_contrast_problems["Elementos con buen contraste de color"])
    
    return {
        "url": url,
        "problems": problems,
        "seo_info": "\n".join(seo_info),
        "heading_hierarchy": heading_hierarchy,
        "heading_issues": heading_issues,
        "sitemap_url": sitemap_url,
        "robots_url": robots_url,
        "aria_roles_info": aria_roles_info
    }



def main():
    url = input("Introduce la URL a analizar: ")
    option = input("¿Quieres analizar solo la URL introducida (1)\nO también analizar las demás URLs con el mismo dominio que se encuentren en la página (2)? ")

    reports = []
    report_files = []

    if option == '1':
        print("Análasis en proceso!")
        response = get_with_retries(url)
        if not response:
            print(f"Error al acceder a {url}")
            return
        soup = BeautifulSoup(response.content, 'html.parser')
        internal_links = [url]  # Solo auditar la URL introducida

    elif option == '2':
        print("Este proceso puede tardar un rato...")
        response = get_with_retries(url)
        if not response:
            print(f"Error al acceder a {url}")
            return
        soup = BeautifulSoup(response.content, 'html.parser')
        internal_links = get_internal_links(url, soup)
    
    for link in internal_links:
        report = analyze_page(link, internal_links)
        if "error" in report:
            print(report["error"])
        else:
            reports.append(report)
            individual_report_path = os.path.join(os.getcwd(), f'report_{hashlib.md5(link.encode()).hexdigest()}.html')
            create_individual_html_report(report, individual_report_path)
            report_files.append(individual_report_path)

    if reports:
        current_date = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        server_os = os.name
        try:
            server_info = response.headers.get('Server', 'Unknown')
        except Exception as e:
            server_info = f"Error getting server info: {e}"
        title = reports[0].get('seo_info', '').split('\n')[0]
        meta_description = reports[0].get('seo_info', '').split('\n')[1] if len(reports[0].get('seo_info', '').split('\n')) > 1 else 'No se encontró ninguna meta descripción'
        sitemap_info = reports[0]['sitemap_url']
        robots_info = reports[0]['robots_url']
        google_analytics_info = "Encontrado" if "Google Analytics: Encontrado" in reports[0]['seo_info'] else "No enocntrado"
        total_urls = len(reports)
        pages_without_title_or_meta = len([r for r in reports if "No se encontró ningún título" in r["seo_info"] or "No se encontró descripción meta" in r["seo_info"]])
        images_without_alt_total = sum(len(r["problems"]["Imágenes sin texto alternativo"]) for r in reports)
        pages_with_slow_load = sum(1 for r in reports if r["problems"]["Tiempo de carga de la página"])
        long_urls_total = sum(1 for r in reports if r["problems"]["URLs demasiado largas"])
        missing_canonical_tags = sum(1 for r in reports if r["problems"]["Canonical tags"])
        missing_hreflang_tags = sum(1 for r in reports if r["problems"]["Hreflang tags"])
        tables_accessibility_issues = sum(len(r["problems"]["Accesibilidad de tablas"]) for r in reports)
        aria_roles_issues = sum(len(r["problems"]["Uso de roles ARIA"]) for r in reports)
        duplicate_content_issues = sum(len(r["problems"]["Posible contenido duplicado"]) for r in reports)
        keyboard_problems_total = sum(len(r["problems"]["Eventos de teclado"]) for r in reports)
        landmarks_problems_total = sum(len(r["problems"]["Landmarks ARIA"]) for r in reports)
        semantic_structure_problems_total = sum(len(r["problems"]["Estructura semántica del documento"]) for r in reports)
        form_accessibility_problems_total = sum(len(r["problems"]["Accesibilidad de formularios"]) for r in reports)
        blinking_content_problems_total = sum(len(r["problems"]["Contenido parpadeante"]) for r in reports)
        svg_accessibility_problems_total = sum(len(r["problems"]["Accesibilidad de SVGs"]) for r in reports)

        total_aria_elements = sum(r["aria_roles_info"]["total_aria_roles"] for r in reports)
        total_role_elements = sum(r["aria_roles_info"]["total_roles"] for r in reports)

        general_info = f'''
        General Information
        Fecha del análisis: {current_date}
        Sistema operativo del servidor: {server_os}
        Servidor web: {server_info}
        Título de la página: {title}
        Meta descripción de la página: {meta_description}
        Sitemap URL: {sitemap_info}
        Robots.txt URL: {robots_info}
        Google Analytics: {google_analytics_info}
        Total URLs Analizadas: {total_urls}
        Total elementos con 'aria': {total_aria_elements}
        Total elementos con 'role': {total_role_elements}
        '''

        if pages_without_title_or_meta > 0:
            general_info += f"Páginas sin título o meta descripción: {pages_without_title_or_meta}"
        if images_without_alt_total > 0:
            general_info += f"Imágenes sin texto alternativo: {images_without_alt_total}"
        if pages_with_slow_load > 0:
            general_info += f"Páginas con tiempo de carga lento: {pages_with_slow_load}"
        if long_urls_total > 0:
            general_info += f"URLs demasiado largas: {long_urls_total}"
        if missing_canonical_tags > 0:
            general_info += f"Faltan etiquetas canonical: {missing_canonical_tags}"
        if missing_hreflang_tags > 0:
            general_info += f"Faltan etiquetas hreflang: {missing_hreflang_tags}"
        if tables_accessibility_issues > 0:
            general_info += f"Problemas de accesibilidad en tablas: {tables_accessibility_issues}"
        if aria_roles_issues > 0:
            general_info += f"Problemas de roles ARIA en elementos interactivos: {aria_roles_issues}"
        if duplicate_content_issues > 0:
            general_info += f"Posible contenido duplicado: {duplicate_content_issues}"
        if keyboard_problems_total > 0:
            general_info += f"Problemas de eventos de teclado: {keyboard_problems_total}"
        if landmarks_problems_total > 0:
            general_info += f"Problemas de landmarks ARIA: {landmarks_problems_total}"
        if semantic_structure_problems_total > 0:
            general_info += f"Problemas de estructura semántica: {semantic_structure_problems_total}"
        if form_accessibility_problems_total > 0:
            general_info += f"Problemas de accesibilidad de formularios: {form_accessibility_problems_total}"
        if blinking_content_problems_total > 0:
            general_info += f"Contenido parpadeante: {blinking_content_problems_total}"
        if svg_accessibility_problems_total > 0:
            general_info += f"Problemas de accesibilidad de SVGs: {svg_accessibility_problems_total}"

        final_output_path = os.path.join(os.getcwd(), 'combined_accessibility_seo_report.html')
        combine_html_reports(report_files, final_output_path, general_info)
        print(f"Auditoría de accesibilidad y SEO completada. El reporte combinado ha sido generado en '{final_output_path}'.")


if __name__ == "__main__":
    main()
