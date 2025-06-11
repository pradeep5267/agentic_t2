import xml.etree.ElementTree as ET
import sys
import os

def convert_kml_to_poly(kml_file, poly_file):
    try:
        ns = {'kml': 'http://www.opengis.net/kml/2.2'}
        tree = ET.parse(kml_file)
        root = tree.getroot()
        
        with open(poly_file, 'w') as f_out:
            placemarks = root.findall('.//kml:Placemark', ns)
            if not placemarks:
                print("Warning: No <Placemark> elements found in the KML file.")
                return

            for placemark in placemarks:
                name_element = placemark.find('kml:name', ns)
                poly_name = name_element.text.strip() if name_element is not None else 'unnamed_polygon'
                
                coords_element = placemark.find('.//kml:coordinates', ns)
                if coords_element is None:
                    continue
                    
                f_out.write(f"{poly_name}\n")
                f_out.write("1\n")
                
                coordinates = coords_element.text.strip().split()
                
                for coord_triple in coordinates:
                    parts = coord_triple.split(',')
                    if len(parts) >= 2:
                        lon, lat = parts[0], parts[1]
                        f_out.write(f"    {lon}   {lat}\n")
                
                # *** CORRECTED SECTION START ***
                f_out.write("END\n") # End of the ring
                f_out.write("END\n") # End of the polygon section
                # *** CORRECTED SECTION END ***
            
        print(f"Successfully converted {len(placemarks)} polygon(s) to {poly_file}")

    except ET.ParseError:
        print(f"Error: Could not parse {kml_file}. Make sure it is a valid XML/KML file.")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")

if __name__ == '__main__':
    if len(sys.argv) != 3:
        print("Usage: python kml_to_poly.py <input_kml_file> <output_poly_file>")
        sys.exit(1)
        
    input_file = sys.argv[1]
    output_file = sys.argv[2]
    
    if not os.path.exists(input_file):
        print(f"Error: Input file not found at '{input_file}'")
        sys.exit(1)
        
    convert_kml_to_poly(input_file, output_file)