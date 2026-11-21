// Sarthi cover letter template. Content comes from sys.inputs as JSON and is shown as plain text.
#let data = json(bytes(sys.inputs.at("data")))
#set document(title: data.name + " - Cover letter", author: data.name)
#set page(paper: data.paper, margin: (x: 2.2cm, y: 2cm))
#set text(font: ("Liberation Sans", "Arial", "Helvetica", "DejaVu Sans", "Libertinus Serif"), size: 10.5pt, lang: "en", ligatures: false)
#set par(justify: false, leading: 0.65em, spacing: 1.1em)

#text(size: 14pt, weight: "bold", data.name) \
#text(size: 9pt, data.contact.join("  |  "))
#v(1.2em)
#data.date
#v(0.8em)
#data.greeting

#for paragraph in data.paragraphs [#paragraph

]
#for line in data.sign_off.split("\n") [#line \ ]
