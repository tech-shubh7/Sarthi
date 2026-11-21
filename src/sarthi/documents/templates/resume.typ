// Sarthi resume template. Single column, real text, no tables or images: what applicant tracking systems read
// best. All content comes from sys.inputs as JSON and is shown as plain text (never evaluated as markup).
#let data = json(bytes(sys.inputs.at("data")))
#set document(title: data.name + " - Resume", author: data.name)
#set page(paper: data.paper, margin: (x: 1.5cm, y: 1.3cm))
#set text(font: ("Liberation Sans", "Arial", "Helvetica", "DejaVu Sans", "Libertinus Serif"), size: 10pt, lang: "en", ligatures: false)
#set par(justify: false, leading: 0.5em, spacing: 0.6em)
#set list(indent: 0.4em, body-indent: 0.5em, spacing: 0.45em)

#let heading-line(title) = {
  v(0.5em)
  text(size: 11pt, weight: "bold", upper(title))
  v(-0.42em)
  line(length: 100%, stroke: 0.5pt + luma(120))
  v(-0.2em)
}
#let when(start, end) = if start != "" or end != "" { [#start#if start != "" and end != "" [ – ]#end] }
#let row(what, dates) = grid(columns: (1fr, auto), column-gutter: 1em, what, align(right, dates))

#align(center)[
  #text(size: 17pt, weight: "bold", data.name) \
  #if data.headline != "" [#text(size: 10.5pt, data.headline) \ ]
  #text(size: 9pt, data.contact.join("  |  "))
]

#if data.summary != "" {
  heading-line("Summary")
  data.summary
}

#if data.experience.len() > 0 {
  heading-line("Experience")
  for job in data.experience {
    row([*#job.title*, #job.company#if job.location != "" [, #job.location]], when(job.start, job.end))
    if job.bullets.len() > 0 { list(..job.bullets.map(b => [#b])) }
    v(0.25em)
  }
}

#if data.projects.len() > 0 {
  heading-line("Projects")
  for project in data.projects {
    [*#project.name*#if project.description != "" [: #project.description]#if project.link != "" [ (#project.link)]]
    if project.bullets.len() > 0 { list(..project.bullets.map(b => [#b])) }
    v(0.2em)
  }
}

#if data.skills.len() > 0 {
  heading-line("Skills")
  for group in data.skills {
    [#if group.label != "" [*#group.label:* ]#group.items.join(", ") \ ]
  }
}

#if data.education.len() > 0 {
  heading-line("Education")
  for school in data.education {
    row([*#school.degree*#if school.degree != "" [, ]#school.school], when(school.start, school.end))
    if school.details.len() > 0 { list(..school.details.map(d => [#d])) }
  }
}

#for extra in data.extras {
  if extra.items.len() > 0 {
    heading-line(extra.heading)
    list(..extra.items.map(i => [#i]))
  }
}
