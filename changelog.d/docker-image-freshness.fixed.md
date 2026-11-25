- A Docker image with its own tag keeps it, and a changed image now reruns the flow. An image whose
  ID cannot be read no longer counts as unchanged. A registry port in an image text, as in
  `localhost:5000/img:tag`, is no longer taken for the tag. The image is checked through the same
  client (`docker`, `podman`) and daemon that the run used.
