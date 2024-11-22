from lumi.utils.image import decode_img
import requests

def get_image(host):
    r = requests.get(f'http://{host}/RHEED/image')
    
    image, image_header = decode_img(r.content, r.headers)
    return image, image_header

