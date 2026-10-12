# Roadmap

Where the project is and what is planned. Milestones 1 to 6 are done; 7 and 8 are next.

1. **Milestone 1 (done):** app, library, transitions, stream, casting, mobile web page, test generator.
2. **Milestone 2 (done):** the real music generator: Stable Audio 3 in its own container on an NVIDIA GPU (or the CPU), scenes by location, mood and tempo, the quality slider and the starter pack.
3. **Milestone 3 (done):** a retro look: the web page has a 1980s 16-bit fantasy style, with scene backgrounds and icons made by the same AI computer.
4. **Milestone 4 (done):** describe a scene in your own words and get music for it, with the same checks the built-in scenes get.
5. **Milestone 5 (done):** favourite and ban tracks, a library page with in-browser preview, export and import of favourites, a size limit you set with a slider (the oldest unplayed music goes first, favourites never), and "more like this" for tracks you liked.
6. **Milestone 6 (done):** player voting. The DM sets the number of players; each opens `/players` on their phone and likes or dislikes the track; when more than half dislike it, it is skipped.
7. **Milestone 7 (investigating):** character leitmotifs. Save a short theme for each character, pick one from a drop-down on a scene, and the generator carries that theme into the new track.
8. **Milestone 8 (investigating):** a different generator backend. Run the generator on rented cloud GPUs (RunPod) for people without an NVIDIA GPU at home or when the home GPU is busy. Open questions: serverless endpoint or an on-demand pod, cold start and cost per track, how the conductor reaches it (the generator API stays the same), and where the model weights live.

Dropped: the mood pad (four scenes in the corners of a square, drag a dot between them).
