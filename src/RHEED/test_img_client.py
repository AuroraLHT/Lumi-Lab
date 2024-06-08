from misc import decode_img
import requests
import matplotlib.pyplot as plt

if __name__ == "__main__":
    host = "127.0.0.1:8000"

    r = requests.get(f'http://{host}/RHEED/image')
    print(r.status_code)

    print(r.headers)

    image, image_header = decode_img(r.content, r.headers)
    plt.imshow(image)
    plt.title(f"time: {image_header['time_stamp']}")
    plt.show()