# import opencv 
import cv2 
  
# Load the input image 
image = cv2.imread('/home/hliang16/Pictures/Wallpapers/IMG_20230518_111128.jpg') 
cv2.imshow('Original', image) 
cv2.waitKey(0) 
  
# Use the cvtColor() function to grayscale the image 
gray_image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) 
print(gray_image.shape)
print(gray_image.max(), gray_image.min())
cv2.imshow('Grayscale', gray_image) 
cv2.waitKey(0)   
  
# Window shown waits for any key pressing event 
cv2.destroyAllWindows()